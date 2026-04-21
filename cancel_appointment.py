"""
Cancel appointment 213393327 and discover the cancellation API endpoint.
"""
import asyncio
import json
import os

from dotenv import load_dotenv
from playwright.async_api import async_playwright, Page, Response, Request

load_dotenv()

EMAIL = os.getenv("HOTDOC_EMAIL")
PASSWORD = os.getenv("HOTDOC_PASSWORD")
BASE_URL = "https://www.hotdoc.com.au"
APPOINTMENT_ID = 213393327


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
    try:
        await page.wait_for_url(lambda url: "login" not in url, timeout=15_000)
    except Exception:
        raise RuntimeError("Login failed")
    await page.wait_for_load_state("networkidle")
    print(f"  Logged in as {EMAIL}")


async def cancel_appointment(page: Page, appointment_id: int) -> None:
    captured_requests = []

    async def log_request(request: Request):
        if "appointment" in request.url.lower() and request.method in ("DELETE", "PATCH", "PUT", "POST"):
            captured_requests.append({
                "method": request.method,
                "url": request.url,
                "post_data": request.post_data,
            })
            print(f"  >> {request.method} {request.url}")
            if request.post_data:
                print(f"     body: {request.post_data[:300]}")

    async def log_response(response: Response):
        if "appointment" in response.url.lower() and response.request.method in ("DELETE", "PATCH", "PUT", "POST"):
            try:
                body = await response.json()
                print(f"  << {response.status} {response.url}")
                print(f"     body: {json.dumps(body)[:300]}")
            except Exception:
                print(f"  << {response.status} {response.url} (non-JSON)")

    page.on("request", log_request)
    page.on("response", log_response)

    print(f"\nNavigating to appointments page...")
    await page.goto(f"{BASE_URL}/medical-centres/account/appointments")
    await page.wait_for_load_state("networkidle")
    await page.wait_for_timeout(2000)

    # Look for the appointment card and cancel button
    print(f"Looking for appointment {appointment_id}...")

    # Try to find cancel button
    cancel_btn = page.locator('button:has-text("Cancel")')
    count = await cancel_btn.count()
    print(f"  Found {count} Cancel button(s)")

    if count > 0:
        print("  Clicking Cancel...")
        await cancel_btn.first.click()
        await page.wait_for_timeout(2000)

        # Confirm cancellation dialog if it appears
        confirm_btn = page.locator('button:has-text("Yes"), button:has-text("Confirm"), button:has-text("Cancel appointment")')
        confirm_count = await confirm_btn.count()
        print(f"  Found {confirm_count} confirmation button(s)")
        if confirm_count > 0:
            print("  Confirming cancellation...")
            await confirm_btn.first.click()
            await page.wait_for_load_state("networkidle")
            await page.wait_for_timeout(3000)

    # Also try direct API call
    print("\nAttempting direct API cancellation...")
    result = await page.evaluate(f"""async () => {{
        const resp = await fetch('/api/patient/appointments/{appointment_id}', {{
            method: 'DELETE',
            headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
            credentials: 'include',
        }});
        return {{ status: resp.status, body: await resp.text() }};
    }}""")
    print(f"  DELETE result: {result}")

    if result['status'] not in (200, 204):
        # Try PATCH with cancellation payload
        result2 = await page.evaluate(f"""async () => {{
            const resp = await fetch('/api/patient/appointments/{appointment_id}', {{
                method: 'PATCH',
                headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
                body: JSON.stringify({{ appointment: {{ state: 'cancelled' }} }}),
                credentials: 'include',
            }});
            return {{ status: resp.status, body: await resp.text() }};
        }}""")
        print(f"  PATCH (state=cancelled) result: {result2}")

        result3 = await page.evaluate(f"""async () => {{
            const resp = await fetch('/api/patient/appointments/{appointment_id}/cancel', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json', 'Accept': 'application/json' }},
                body: JSON.stringify({{}}),
                credentials: 'include',
            }});
            return {{ status: resp.status, body: await resp.text() }};
        }}""")
        print(f"  POST /cancel result: {result3}")

    print(f"\nCaptured requests: {json.dumps(captured_requests, indent=2)}")


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
        await cancel_appointment(page, APPOINTMENT_ID)

        input("\nPress Enter to close browser...")
        await browser.close()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
