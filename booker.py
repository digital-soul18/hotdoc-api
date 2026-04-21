"""
booker.py — Book the nurse at Lifeline Family Doctors (3+ days away) then cancel.

Usage:
    python booker.py
    python booker.py --book-only      # skip cancellation
    python booker.py --cancel-only    # cancel last known appointment
    python booker.py --appointment-id 213393327  # cancel a specific appointment

=============================================================================
ARCHITECTURE NOTES (learned from reverse-engineering HotDoc)
=============================================================================

HotDoc is an Ember.js SPA. Two distinct booking flows exist:

1. CONSULT FLOW  /request/consult/start?defaults=practice-{clinic}%2Cpractitioner-{doctor}
   Steps: For myself → Existing patient → Agree → [consultation type] → [reason-message?] → doctor-time
   This is the flow used by this script. It preserves the doctor slug in the URL through
   all steps, which ensures the correct availability_type_ids are shown for that doctor.

2. DIRECT FLOW  /request/appointment/start?clinic=...&doctor=...&when=...
   Used by the "link" field in time_slot API responses. Skips the consultation-type step.
   Not used here because it requires knowing the exact start_time in advance.

KEY API ENDPOINTS
-----------------
GET  /api/patient/time_slots
     ?start_time=...&end_time=...&timezone=Australia/Sydney
     &clinic_id=16940&availability_type_ids[]=...&doctor_ids[]=...
     &booked_for_id=1549419&booked_for_type=patient
     → Returns list of available slots (5-day windows; paginate with "next" button)

POST /api/patient/appointments       → 201, body: { appointment: { id: ... } }
     Booking is created when user clicks "Yes, book" on the review step.

PUT  /api/patient/activity_items/appointment-{id}/cancel  → 200
     This is the cancellation endpoint. It CANNOT be called via page.evaluate/fetch()
     because the Ember SPA uses its own internal token scheme that isn't accessible
     from JavaScript fetch(). It must be triggered by clicking the Cancel button in
     the UI at /medical-centres/account/appointments — Ember fires it automatically.
     Trying DELETE /api/patient/appointments/{id} returns 404.

CLINIC / DOCTOR IDs
--------------------
Clinic ID:    16940   (lifeline-family-doctors, Blacktown NSW 2148)
Patient ID:   1549419 (sowrabh@digital-soul.com.au account)
Nurse ID:     216992  (Diksha Shayal)
Nurse slug:   diksha-shayal-nurse-book-here-for-iron-infusion-flu-vaccine

AVAILABILITY TYPES (nurse)
--------------------------
1201624  Wound dressing/review  (reason=202381)  ← used by this script; has online slots
1411646  Iron infusion          (different reason)
Standard appt. (reason=199261 → availability_type_id=1188948) is for Dr Patel, NOT the nurse.
The nurse's "Standard appt." shows "Call to discuss" with zero online slots.

TIME SLOT RESPONSE SHAPE
-------------------------
{
  "id": "1181889-1776731400",
  "day": "2026-04-24",
  "label": "9:15 am",
  "start_time": "2026-04-24T09:15:00+10:00",
  "end_time": "2026-04-24T09:45:00+10:00",
  "duration": 1800,
  "availability_type_id": "1201624",
  "link": "https://www.hotdoc.com.au/medical-centres/book/appointment/start?clinic=16940&..."
}

SLOT BUTTON aria-label FORMAT
-------------------------------
Buttons on the doctor-time page have aria-label="9:15 am Friday 24 April" (no year, no comma).
Pattern: "{label} {weekday} {day-of-month} {month-name}"

LOGIN QUIRK
-----------
After clicking Submit on the login form, wait_for_load_state("networkidle") resolves
BEFORE Ember navigates away from /medical-centres/login. Must use:
    page.wait_for_url(lambda url: "login" not in url, timeout=15_000)

TESTED APPOINTMENTS (all subsequently cancelled)
-------------------------------------------------
213393327  Thu 23 Apr 2026 10:30am  Wound dressing/review  (booked/cancelled manually)
213393596  Fri 24 Apr 2026  9:15am  Wound dressing/review  (booked by booker.py run 1)
213393664  Fri 24 Apr 2026  9:15am  Wound dressing/review  (booked by booker.py run 2)
213393738  Fri 24 Apr 2026  9:15am  Wound dressing/review  (booked by booker.py run 3)
=============================================================================
"""
import asyncio
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import argparse

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Response

load_dotenv()

EMAIL = os.getenv("HOTDOC_EMAIL")
PASSWORD = os.getenv("HOTDOC_PASSWORD")
BASE_URL = "https://www.hotdoc.com.au"

# Nurse at Lifeline Family Doctors Blacktown
CLINIC_SLUG = "lifeline-family-doctors"
NURSE_SLUG = "diksha-shayal-nurse-book-here-for-iron-infusion-flu-vaccine"

SYDNEY_TZ = ZoneInfo("Australia/Sydney")
MIN_DAYS_AHEAD = 3


# ---------------------------------------------------------------------------
# Login
# ---------------------------------------------------------------------------

async def login(page: Page) -> None:
    print("Logging in...")
    await page.goto(f"{BASE_URL}/medical-centres/login")
    await page.wait_for_load_state("domcontentloaded")
    if "login" not in page.url:
        print("  Already logged in")
        return
    await page.fill('input[type="email"]', EMAIL)
    await page.fill('input[type="password"]', PASSWORD)
    await page.click('button[type="submit"]')
    # Ember SPA navigates away before networkidle fires — must watch the URL
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
    except Exception:
        raise RuntimeError("Login failed — check credentials in .env")
    await page.wait_for_load_state("networkidle")
    print(f"  Logged in as {EMAIL}")


# ---------------------------------------------------------------------------
# Find a nurse slot 3+ days away
# ---------------------------------------------------------------------------

async def find_nurse_slot(page: Page) -> dict | None:
    """Navigate the consult flow for the nurse and return first slot 3+ days ahead."""
    print("\nFinding nurse slots (3+ days away)...")

    collected_slots: list[dict] = []
    cutoff = datetime.now(SYDNEY_TZ) + timedelta(days=MIN_DAYS_AHEAD)

    async def capture(response: Response):
        if "/api/patient/time_slots" in response.url and response.status == 200:
            try:
                data = await response.json()
                slots = data if isinstance(data, list) else data.get("time_slots", [])
                for s in slots:
                    start = datetime.fromisoformat(s.get("start_time", ""))
                    if start >= cutoff:
                        collected_slots.append(s)
            except Exception as e:
                print(f"    [time_slots] parse error: {e}")

    page.on("response", capture)

    try:
        start_url = (
            f"{BASE_URL}/request/consult/start?"
            f"defaults=practice-{CLINIC_SLUG}%2Cpractitioner-{NURSE_SLUG}"
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

        # "Wound dressing/review" has online slots for the nurse; Standard appt. does not
        picked = await _click(
            page,
            ["Wound dressing/review", "Wound dressing", "Standard appt.", "Standard consultation"],
            fallback_first=True,
        )
        if not picked:
            print("  No consultation type found for nurse — aborting")
            return None

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        if "reason-message" in page.url:
            await _click(page, ["Continue"])
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

        # Paginate through up to 8 weeks to find a slot 3+ days out
        for _ in range(8):
            if collected_slots:
                break
            clicked = await _click(page, ["next", "Later days"])
            if not clicked:
                break
            await page.wait_for_timeout(1200)

    finally:
        page.remove_listener("response", capture)

    if not collected_slots:
        print(f"  No slots found 3+ days ahead (cutoff: {cutoff.strftime('%Y-%m-%d %H:%M')})")
        return None

    collected_slots.sort(key=lambda s: s["start_time"])
    slot = collected_slots[0]
    slot_dt = datetime.fromisoformat(slot["start_time"])
    print(f"  Selected slot: {slot['label']} on {slot_dt.strftime('%A %-d %B %Y')}")
    return slot


# ---------------------------------------------------------------------------
# Book the slot
# ---------------------------------------------------------------------------

async def book_slot(page: Page, slot: dict) -> int | None:
    """
    Re-navigate the consult flow, click the matching time slot button, step through
    stipulations ("No" to COVID symptoms) and the review page ("Yes, book"), then
    return the new appointment ID captured from POST /api/patient/appointments → 201.
    """
    slot_dt = datetime.fromisoformat(slot["start_time"])
    # aria-label format on slot buttons: "9:15 am Friday 24 April" (no year, no comma)
    # aria-label format: "2:15 pm Thursday May 7"  (month before day — HotDoc's format)
    aria_label = f"{slot['label']} {slot_dt.strftime('%A %B %-d')}"

    print(f"\nBooking slot '{aria_label}'...")

    appointment_id: int | None = None

    async def capture_booking(response: Response):
        nonlocal appointment_id
        if "/api/patient/appointments" in response.url and response.status == 201:
            try:
                data = await response.json()
                appt = data.get("appointment") or data
                appointment_id = appt.get("id") or appt.get("appointment_id")
                print(f"  >> Appointment created: ID={appointment_id}")
            except Exception as e:
                print(f"  >> Booking response parse error: {e}")

    page.on("response", capture_booking)

    try:
        start_url = (
            f"{BASE_URL}/request/consult/start?"
            f"defaults=practice-{CLINIC_SLUG}%2Cpractitioner-{NURSE_SLUG}"
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

        await _click(page, ["Wound dressing/review", "Wound dressing", "Standard appt.", "Standard consultation"], fallback_first=True)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        if "reason-message" in page.url:
            await _click(page, ["Continue"])
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

        # Paginate until the target week is visible
        for _ in range(10):
            btn = page.locator(f'[aria-label*="{slot["label"]}"][aria-label*="{slot_dt.strftime("%A")}"]')
            if await btn.count() == 0:
                btn = page.locator(f'button:has-text("{slot["label"]}")')
            if await btn.count() > 0:
                break
            if not await _click(page, ["next", "Later days"]):
                break
            await page.wait_for_timeout(1200)

        # Click the slot button — try progressively looser matches
        clicked = False
        for selector in [
            f'[aria-label*="{aria_label}"]',
            f'[aria-label*="{slot["label"]}"][aria-label*="{slot_dt.strftime("%A")}"]',
            f'button:has-text("{slot["label"]}")',
        ]:
            btn = page.locator(selector)
            if await btn.count() > 0:
                await btn.first.click()
                clicked = True
                break

        if not clicked:
            print(f"  Could not find slot button for '{aria_label}'")
            return None

        await page.wait_for_timeout(1500)
        await page.wait_for_load_state("networkidle")

        # Step through any intermediate pages before the final review:
        #   terms-and-conditions  → "I have read and agree"  (e.g. Dr Patel)
        #   stipulations / symptoms → "No"                   (e.g. Nurse)
        for _ in range(6):
            url = page.url
            if "terms-and-conditions" in url:
                await _click(page, ["I have read and agree"])
            elif "stipulation" in url or "symptom" in url or "covid" in url.lower():
                await _click(page, ["No"])
            else:
                break
            await page.wait_for_timeout(1000)
            await page.wait_for_load_state("networkidle")

        # Review — confirm booking
        await _click(page, ["Yes, book", "Confirm", "Book appointment"])
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(3000)

    finally:
        page.remove_listener("response", capture_booking)

    return appointment_id


# ---------------------------------------------------------------------------
# Cancel appointment
# ---------------------------------------------------------------------------

async def cancel_appointment(page: Page, appointment_id: int) -> bool:
    """
    Cancel via the appointments page UI.

    The real API call is PUT /api/patient/activity_items/appointment-{id}/cancel → 200,
    but this endpoint cannot be called directly via fetch() / page.evaluate() —
    Ember uses an internal token that only the SPA's own XHR carries.
    Navigating to the appointments page and clicking Cancel is the only reliable path.
    """
    print(f"\nCancelling appointment {appointment_id}...")

    await page.goto(f"{BASE_URL}/medical-centres/account/appointments")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(1500)

    cancel_btn = page.locator('button:has-text("Cancel")')
    if await cancel_btn.count() == 0:
        print(f"  No Cancel button found — appointment may already be cancelled")
        return False

    await cancel_btn.first.click()
    await page.wait_for_timeout(1500)

    confirm_btn = page.locator(
        'button:has-text("Yes"), button:has-text("Confirm"), button:has-text("Cancel appointment")'
    )
    if await confirm_btn.count() > 0:
        await confirm_btn.first.click()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(2000)

    print(f"  Appointment {appointment_id} cancelled")
    return True


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
# Main
# ---------------------------------------------------------------------------

async def main(args: argparse.Namespace) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False, slow_mo=80)
        context = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = await context.new_page()

        await login(page)

        if args.cancel_only:
            if not args.appointment_id:
                print("--cancel-only requires --appointment-id")
                sys.exit(1)
            await cancel_appointment(page, args.appointment_id)
            await browser.close()
            return

        slot = await find_nurse_slot(page)
        if not slot:
            print("No suitable slot found — exiting.")
            await browser.close()
            return

        appointment_id = await book_slot(page, slot)
        if not appointment_id:
            print("Booking failed — could not obtain appointment ID.")
            await browser.close()
            return

        print(f"\nBooked! Appointment ID: {appointment_id}")

        if args.book_only:
            print("--book-only flag set, skipping cancellation.")
            await browser.close()
            return

        await cancel_appointment(page, appointment_id)

        await browser.close()
        print("\nDone.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="HotDoc nurse appointment booker + canceller")
    parser.add_argument("--book-only", action="store_true", help="Book but do not cancel")
    parser.add_argument("--cancel-only", action="store_true", help="Cancel an existing appointment")
    parser.add_argument("--appointment-id", type=int, help="Appointment ID (for --cancel-only)")
    args = parser.parse_args()
    asyncio.run(main(args))
