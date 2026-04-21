"""
api.py — HotDoc booking API

POST /book    → login, navigate to practitioner, book a specific slot
POST /cancel  → login, find appointment by time (or ID), cancel it

Deploy to Railway with the included Dockerfile.
Env vars required: HOTDOC_EMAIL, HOTDOC_PASSWORD
"""
import os
import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Response

load_dotenv()

EMAIL = os.getenv("HOTDOC_EMAIL")
PASSWORD = os.getenv("HOTDOC_PASSWORD")
BASE_URL = "https://www.hotdoc.com.au"
CLINIC_SLUG = "lifeline-family-doctors"
SYDNEY_TZ = ZoneInfo("Australia/Sydney")

# Known practitioners — add more as needed
PRACTITIONER_SLUGS = {
    "nurse": "diksha-shayal-nurse-book-here-for-iron-infusion-flu-vaccine",
    "diksha": "diksha-shayal-nurse-book-here-for-iron-infusion-flu-vaccine",
    # Add GP slugs here as discovered, e.g.:
    # "patel": "dr-patel-slug-here",
}

app = FastAPI(title="HotDoc Booking API")


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class BookRequest(BaseModel):
    practitioner: str         # "nurse", "diksha", or a full slug
    date: str                 # "2026-04-24"  (YYYY-MM-DD, Sydney time)
    time: str                 # "9:15 am"     (label as returned by time_slots API)
    reason: Optional[str] = None  # consultation type label, e.g. "Wound dressing/review"

class BookResponse(BaseModel):
    appointment_id: int
    practitioner: str
    date: str
    time: str
    message: str

class CancelRequest(BaseModel):
    appointment_id: Optional[int] = None  # preferred — cancel by ID
    time: Optional[str] = None            # fallback — cancel by time label, e.g. "9:15 AM"
    date: Optional[str] = None            # narrow by date when using time fallback

class CancelResponse(BaseModel):
    cancelled: bool
    appointment_id: Optional[int]
    message: str


# ---------------------------------------------------------------------------
# Shared Playwright helpers
# ---------------------------------------------------------------------------

async def _make_browser():
    playwright = await async_playwright().start()
    browser = await playwright.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-setuid-sandbox"],
    )
    context = await browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
    )
    page = await context.new_page()
    return playwright, browser, page


async def _login(page: Page) -> None:
    await page.goto(f"{BASE_URL}/medical-centres/login")
    await page.wait_for_load_state("domcontentloaded")
    if "login" not in page.url:
        return
    await page.fill('input[type="email"]', EMAIL)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
    except Exception:
        raise HTTPException(status_code=401, detail="HotDoc login failed — check credentials")
    await page.wait_for_load_state("networkidle")


async def _click(page: Page, labels: list[str], fallback_first: bool = False) -> bool:
    for label in labels:
        btn = page.locator(f'button:has-text("{label}")')
        if await btn.count() > 0:
            await btn.first.click()
            return True
    if fallback_first:
        btn = page.locator('main button:not(:has-text("Back")):not(:has-text("Close"))')
        if await btn.count() > 0:
            await btn.first.click()
            return True
    return False


# ---------------------------------------------------------------------------
# Book endpoint
# ---------------------------------------------------------------------------

@app.post("/book", response_model=BookResponse)
async def book(req: BookRequest):
    """
    Book an appointment with the given practitioner at the specified date + time.

    Example body:
        { "practitioner": "nurse", "date": "2026-04-24", "time": "9:15 am" }
    """
    # Resolve practitioner slug
    slug = PRACTITIONER_SLUGS.get(req.practitioner.lower(), req.practitioner)

    # Parse date to build aria-label
    try:
        slot_dt = datetime.strptime(req.date, "%Y-%m-%d").replace(tzinfo=SYDNEY_TZ)
    except ValueError:
        raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")

    # aria-label format: "9:15 am Friday 24 April"
    aria_label = f"{req.time} {slot_dt.strftime('%A %-d %B')}"

    appointment_id: int | None = None

    playwright, browser, page = await _make_browser()
    try:
        await _login(page)

        async def capture_booking(response: Response):
            nonlocal appointment_id
            if "/api/patient/appointments" in response.url and response.status == 201:
                try:
                    data = await response.json()
                    appt = data.get("appointment") or data
                    appointment_id = appt.get("id") or appt.get("appointment_id")
                except Exception:
                    pass

        page.on("response", capture_booking)

        start_url = (
            f"{BASE_URL}/request/consult/start?"
            f"defaults=practice-{CLINIC_SLUG}%2Cpractitioner-{slug}"
        )
        await page.goto(start_url)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        await _click(page, ["For myself"])
        await page.wait_for_timeout(1000)
        await _click(page, ["Existing patient"])
        await page.wait_for_timeout(1000)
        await _click(page, ["Agree"])
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # Consultation type — use req.reason if provided, else sensible defaults
        reason_labels = (
            [req.reason] if req.reason
            else ["Wound dressing/review", "Wound dressing", "Standard appt.", "Standard consultation"]
        )
        if not await _click(page, reason_labels, fallback_first=True):
            raise HTTPException(status_code=422, detail="No matching consultation type found")

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        if "reason-message" in page.url:
            await _click(page, ["Continue"])
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

        # Paginate until the target week is visible
        for _ in range(10):
            btn = page.locator(f'[aria-label*="{req.time}"][aria-label*="{slot_dt.strftime("%A")}"]')
            if await btn.count() > 0:
                break
            if not await _click(page, ["next", "Later days"]):
                break
            await page.wait_for_timeout(1200)

        # Click the slot
        clicked = False
        for selector in [
            f'[aria-label*="{aria_label}"]',
            f'[aria-label*="{req.time}"][aria-label*="{slot_dt.strftime("%A")}"]',
            f'button:has-text("{req.time}")',
        ]:
            btn = page.locator(selector)
            if await btn.count() > 0:
                await btn.first.click()
                clicked = True
                break

        if not clicked:
            raise HTTPException(
                status_code=404,
                detail=f"No available slot found for '{req.time}' on {req.date}"
            )

        await page.wait_for_timeout(1500)
        await page.wait_for_load_state("networkidle")

        # Stipulations — answer "No"
        for _ in range(3):
            if "stipulation" in page.url or "symptom" in page.url:
                await _click(page, ["No"])
                await page.wait_for_timeout(1000)
                await page.wait_for_load_state("networkidle")
            else:
                break

        # Review — confirm
        await _click(page, ["Yes, book", "Confirm", "Book appointment"])
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

        page.remove_listener("response", capture_booking)

    finally:
        await browser.close()
        await playwright.stop()

    if not appointment_id:
        raise HTTPException(status_code=500, detail="Booking flow completed but no appointment ID captured")

    return BookResponse(
        appointment_id=appointment_id,
        practitioner=req.practitioner,
        date=req.date,
        time=req.time,
        message=f"Appointment {appointment_id} booked for {req.time} on {req.date}",
    )


# ---------------------------------------------------------------------------
# Cancel endpoint
# ---------------------------------------------------------------------------

@app.post("/cancel", response_model=CancelResponse)
async def cancel(req: CancelRequest):
    """
    Cancel an appointment by ID or by matching date/time on the appointments page.

    Examples:
        { "appointment_id": 213393664 }
        { "time": "9:15 AM", "date": "2026-04-24" }
        { "time": "9:15 AM" }   # cancels first match
    """
    if not req.appointment_id and not req.time:
        raise HTTPException(status_code=422, detail="Provide appointment_id or time")

    playwright, browser, page = await _make_browser()
    cancelled_id: int | None = req.appointment_id

    try:
        await _login(page)

        async def log_cancel(response: Response):
            nonlocal cancelled_id
            if "activity_items" in response.url and "cancel" in response.url and response.status == 200:
                # Extract ID from URL: /activity_items/appointment-213393664/cancel
                try:
                    seg = response.url.split("appointment-")[1].split("/")[0]
                    if seg.isdigit():
                        cancelled_id = int(seg)
                except Exception:
                    pass

        page.on("response", log_cancel)

        await page.goto(f"{BASE_URL}/medical-centres/account/appointments")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        # Find the right Cancel button
        if req.time:
            # Locate the appointment card containing this time, then its Cancel button
            time_upper = req.time.upper().replace("AM", "AM").replace("PM", "PM")
            # Try to find a card section containing the time text
            card = page.locator(f'[class*="appointment"]:has-text("{req.time}"), '
                                f'[class*="card"]:has-text("{req.time}"), '
                                f'section:has-text("{req.time}"), '
                                f'li:has-text("{req.time}"), '
                                f'div:has-text("{time_upper}")')
            # Narrow by date if provided
            if req.date:
                try:
                    dt = datetime.strptime(req.date, "%Y-%m-%d")
                    # HotDoc shows date as "23 Apr" or "Thursday 23 Apr"
                    date_text = dt.strftime("%-d %b")
                    card = page.locator(f'div:has-text("{date_text}"):has-text("{req.time}")')
                except ValueError:
                    pass

            # Try to click Cancel inside the matching card
            cancel_in_card = card.locator('button:has-text("Cancel")').first
            if await cancel_in_card.count() > 0:
                await cancel_in_card.click()
            else:
                # Fallback: first Cancel button on the page
                cancel_btn = page.locator('button:has-text("Cancel")').first
                if await cancel_btn.count() == 0:
                    raise HTTPException(status_code=404, detail="No matching appointment found to cancel")
                await cancel_btn.click()
        else:
            # Cancel by appointment_id — just use first Cancel button
            # (works reliably when there's one upcoming appointment)
            cancel_btn = page.locator('button:has-text("Cancel")').first
            if await cancel_btn.count() == 0:
                raise HTTPException(status_code=404, detail="No upcoming appointments found")
            await cancel_btn.click()

        await page.wait_for_timeout(1500)

        # Confirm dialog
        confirm_btn = page.locator(
            'button:has-text("Yes"), button:has-text("Confirm"), button:has-text("Cancel appointment")'
        )
        if await confirm_btn.count() > 0:
            await confirm_btn.first.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

        page.remove_listener("response", log_cancel)

    finally:
        await browser.close()
        await playwright.stop()

    return CancelResponse(
        cancelled=True,
        appointment_id=cancelled_id,
        message=f"Appointment {cancelled_id} cancelled successfully",
    )


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}
