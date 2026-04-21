import asyncio
import json
import os
import csv
from datetime import datetime
from urllib.parse import urlparse, parse_qs

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Response

load_dotenv()

EMAIL = os.getenv("HOTDOC_EMAIL")
PASSWORD = os.getenv("HOTDOC_PASSWORD")
CLINIC_URL = os.getenv(
    "HOTDOC_CLINIC_URL",
    "https://www.hotdoc.com.au/medical-centres/blacktown-NSW-2148/lifeline-family-doctors/doctors",
)

BASE_URL = "https://www.hotdoc.com.au"
WEEKS_TO_SCRAPE = 6  # How many 5-day windows to fetch per doctor


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

    # Ember SPA — wait for URL to navigate away from the login page
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
    except Exception:
        raise RuntimeError("Login failed — check credentials in .env")

    await page.wait_for_load_state("networkidle")
    print(f"  Logged in as {EMAIL}")


# ---------------------------------------------------------------------------
# Get patient ID
# ---------------------------------------------------------------------------

async def get_patient_id(page: Page) -> str:
    patient_id = None

    async def capture(response: Response):
        nonlocal patient_id
        if "/api/patient/patients/" in response.url and response.status == 200:
            seg = response.url.split("/api/patient/patients/")[1].split("?")[0].split("/")[0]
            if seg.isdigit():
                patient_id = seg

    page.on("response", capture)
    await page.goto(BASE_URL)
    await page.wait_for_load_state("networkidle")
    page.remove_listener("response", capture)

    if not patient_id:
        raise RuntimeError("Could not determine patient ID — are you logged in?")
    print(f"  Patient ID: {patient_id}")
    return patient_id


# ---------------------------------------------------------------------------
# Scrape doctor list
# ---------------------------------------------------------------------------

async def get_doctors(page: Page) -> list[dict]:
    print(f"\nFetching doctor list from {CLINIC_URL} ...")
    await page.goto(CLINIC_URL)
    await page.wait_for_load_state("networkidle")

    # Clinic slug is the 4th path segment: /medical-centres/{suburb}/{clinic-slug}/doctors
    clinic_slug = CLINIC_URL.replace(BASE_URL, "").split("/")[3]

    links = await page.query_selector_all('a[href*="/doctors/"]')
    doctors, seen = [], set()

    for link in links:
        href = await link.get_attribute("href") or ""
        parts = href.split("/doctors/")
        if len(parts) < 2 or not parts[1]:
            continue
        slug = parts[1].rstrip("/")
        if slug in seen:
            continue
        seen.add(slug)

        # The link itself contains the doctor's display name
        raw_name = (await link.inner_text()).strip()
        name = raw_name if raw_name else slug

        doctors.append({"name": name, "slug": slug, "clinic_slug": clinic_slug})

    print(f"  Found {len(doctors)} doctors: {[d['name'] for d in doctors]}")
    return doctors


# ---------------------------------------------------------------------------
# Click helper
# ---------------------------------------------------------------------------

async def click_button(page: Page, labels: list[str], fallback_first: bool = False) -> bool:
    for label in labels:
        btn = page.locator(f'button:has-text("{label}")')
        if await btn.count() > 0:
            await btn.first.click()
            return True
    if fallback_first:
        # Skip Back/Close navigation buttons
        btn = page.locator('main button:not(:has-text("Back")):not(:has-text("Close"))')
        if await btn.count() > 0:
            await btn.first.click()
            return True
    return False


# ---------------------------------------------------------------------------
# Scrape time slots for one doctor
# ---------------------------------------------------------------------------

async def scrape_doctor(page: Page, doctor: dict, patient_id: str) -> list[dict]:
    print(f"\n  Doctor: {doctor['name']}")
    clinic_slug = doctor["clinic_slug"]
    doctor_slug = doctor["slug"]
    all_slots: list[dict] = []

    async def capture_slots(response: Response):
        if "/api/patient/time_slots" in response.url and response.status == 200:
            try:
                data = await response.json()
                # Response is either a list of slots or {time_slots: [...]}
                slots = data if isinstance(data, list) else data.get("time_slots", data.get("slots", []))
                if isinstance(slots, list):
                    for s in slots:
                        s["_doctor"] = doctor["name"]
                    all_slots.extend(slots)
                    print(f"    [time_slots] {len(slots)} slots ({response.url.split('start_time=')[1][:10]}...)")
            except Exception as e:
                print(f"    [time_slots] parse error: {e}")

    page.on("response", capture_slots)

    try:
        # ── Step 1: booking start ──────────────────────────────────────────
        start_url = (
            f"{BASE_URL}/request/consult/start?"
            f"defaults=practice-{clinic_slug}%2Cpractitioner-{doctor_slug}"
        )
        await page.goto(start_url)
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # ── Step 2: "For myself" ──────────────────────────────────────────
        await click_button(page, ["For myself"])
        await page.wait_for_timeout(1000)

        # ── Step 3: "Existing patient" ────────────────────────────────────
        await click_button(page, ["Existing patient"])
        await page.wait_for_timeout(1000)

        # ── Step 4: "Agree" (T&Cs) ────────────────────────────────────────
        await click_button(page, ["Agree"])
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # ── Step 5: consultation type — prefer "Standard appt." ───────────
        picked = await click_button(
            page,
            ["Standard appt.", "Standard consultation", "General appointment", "General consult"],
            fallback_first=True,
        )
        if not picked:
            print(f"    No consultation type found, skipping {doctor['name']}")
            return []

        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1500)

        # ── Step 6: "Continue" on reason-message step (if present) ────────
        if "reason-message" in page.url:
            await click_button(page, ["Continue"])
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(2000)

        # ── Step 7: now on doctor-time — initial slots already captured ───
        # Click "next" several times to get future weeks
        for week in range(WEEKS_TO_SCRAPE - 1):
            clicked = await click_button(page, ["next", "Later days"])
            if not clicked:
                break
            await page.wait_for_timeout(1200)

    finally:
        page.remove_listener("response", capture_slots)

    print(f"    Total: {len(all_slots)} slots for {doctor['name']}")
    return all_slots


# ---------------------------------------------------------------------------
# Save outputs
# ---------------------------------------------------------------------------

def save_outputs(all_slots: list[dict]) -> None:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    raw_path = f"schedule_raw_{ts}.json"
    with open(raw_path, "w") as f:
        json.dump(all_slots, f, indent=2)
    print(f"\n  Raw JSON  → {raw_path}")

    if all_slots and isinstance(all_slots[0], dict):
        csv_path = f"schedule_{ts}.csv"
        fieldnames = list(all_slots[0].keys())
        with open(csv_path, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(all_slots)
        print(f"  CSV       → {csv_path}")
    else:
        print("  No slots captured — inspect the raw JSON for API response shapes")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

async def main():
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
        patient_id = await get_patient_id(page)
        doctors = await get_doctors(page)

        all_slots: list[dict] = []
        for doctor in doctors:
            slots = await scrape_doctor(page, doctor, patient_id)
            all_slots.extend(slots)

        print(f"\nTotal slots scraped: {len(all_slots)}")
        save_outputs(all_slots)

        await browser.close()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
