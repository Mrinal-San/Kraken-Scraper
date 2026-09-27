from playwright.sync_api import sync_playwright
from datetime import date, timedelta
import time
from pathlib import Path

if __package__:
	from .feature_engineering import parse_flight_text, write_flight_rows_to_csv
else:
	from feature_engineering import parse_flight_text, write_flight_rows_to_csv


def is_challenge_page(page):
	title = page.title().lower()
	url = page.url.lower()
	return (
		any(marker in title for marker in ("challenge", "attention", "validation", "captcha"))
		or any(marker in url for marker in ("akamai", "bot", "challenge", "captcha"))
	)


def pause_for_challenge_if_needed(page):
	if not is_challenge_page(page):
		return

	print("A Yatra/Akamai challenge was detected.")
	print("Solve it manually in the visible browser, then click Resume in Playwright Inspector.")
	page.pause()

	print("Resumed. Waiting for the challenge page to clear...")
	deadline = time.monotonic() + 300
	while time.monotonic() < deadline:
		if not is_challenge_page(page):
			print(f"Challenge cleared. Continuing at {page.url}")
			return
		page.wait_for_timeout(500)

	raise TimeoutError("The challenge was not cleared within 5 minutes.")


def collect_flights_while_scrolling(page):
	flights_by_number = {}
	previous_height = None
	previous_flight_count = 0
	stable_bottom_rounds = 0

	for _ in range(60):
		page.locator("body").wait_for(state="visible", timeout=30000)
		for row in parse_flight_text(page.locator("body").inner_text()):
			flights_by_number.setdefault(row["flight_number"], row)

		page.evaluate(
			"""() => {
				const step = Math.max(window.innerHeight * 0.8, 400);
				window.scrollBy(0, step);
			}"""
		)
		page.wait_for_timeout(700)

		for row in parse_flight_text(page.locator("body").inner_text()):
			flights_by_number.setdefault(row["flight_number"], row)
		metrics = page.evaluate(
			"""() => ({
				height: document.documentElement.scrollHeight,
				y: window.scrollY,
				viewport: window.innerHeight
			})"""
		)
		at_bottom = metrics["y"] + metrics["viewport"] >= metrics["height"] - 4
		flight_count = len(flights_by_number)
		if (
			at_bottom
			and metrics["height"] == previous_height
			and flight_count == previous_flight_count
		):
			stable_bottom_rounds += 1
		else:
			stable_bottom_rounds = 0

		if stable_bottom_rounds >= 3:
			break
		previous_height = metrics["height"]
		previous_flight_count = flight_count

	return list(flights_by_number.values())


pw = sync_playwright().start()
browser = pw.firefox.launch(headless = False, slow_mo = 2000)
page = browser.new_page()
page.goto("https://www.yatra.com", wait_until="domcontentloaded", timeout=6000)
pause_for_challenge_if_needed(page)

# ==========================================
# 1. HANDLE DEPARTURE (Origin)
# ==========================================

departure_field = page.locator('[role="Combobox"][aria-label^="Departure From"]')
departure_field.click()
city_input = page.locator("#input-with-icon-adornment")
city_input.fill("Mumbai")
page.get_by_text("Mumbai", exact=True).first.click()
print("✅ Departure changed to Mumbai successfully!")




# ==========================================
# 2. HANDLE ARRIVAL (Destination)
# ==========================================
city_input = page.locator("#input-with-icon-adornment")
city_input.fill("New Delhi")
page.get_by_text("New Delhi", exact=True).first.click()
print("✅ Destination changed to New Delhi successfully!")


# ==========================================
# 3. HANDLE DEPARTURE DATE
# ==========================================
tomorrow = date.today() + timedelta(days=1)
day = tomorrow.day
if 10 <= day % 100 <= 20:
	ordinal = "th"
else:
	ordinal = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
date_label = f"Choose {tomorrow.strftime('%A, %B')} {day}{ordinal}, {tomorrow.year}"
calendar_day = page.locator(
	f'.react-datepicker__day:not(.react-datepicker__day--outside-month)'
	f'[aria-label="{date_label}"][aria-disabled="false"]'
)
calendar_day.wait_for(state="visible", timeout=10000)
calendar_day.click()
page.wait_for_timeout(500)

# ==========================================
# 4. SELECT PREMIUM ECONOMY
# ==========================================
page.locator('[aria-label="Travellers class inputbox"]').click()
premium_economy = page.locator('label[role="radio"][aria-label="Premium Economy"]')
premium_economy.focus()
premium_economy.press("Space")
if not page.locator('input[type="radio"][value="Special"]').is_checked():
	raise RuntimeError("Premium Economy was not selected")
page.get_by_role("button", name="Done", exact=True).click()

# ==========================================
# 5. HANDLE SEARCH BUTTON & REDIRECT
# ==========================================
search_button = page.get_by_role("button", name="Search", exact=True)
opened_pages = []


def capture_opened_page(opened_page):
	opened_pages.append(opened_page)


context = page.context
context.on("page", capture_opened_page)
try:
	search_button.click()
	page.wait_for_timeout(1500)
finally:
	context.remove_listener("page", capture_opened_page)

if opened_pages:
	page = opened_pages[-1]
	page.wait_for_load_state("domcontentloaded", timeout=30000)
	print(f"Results opened in a new tab: {page.url}")

pause_for_challenge_if_needed(page)

print(page.title())
page.wait_for_timeout(1500)
print("Scrolling through flight results and collecting loaded flights...")
flight_rows = collect_flights_while_scrolling(page)
results_text = page.locator("body").inner_text()
with open("yatra_scrape.txt", "w", encoding="utf-8") as results_file:
	results_file.write(results_text)
print("Page text saved to yatra_scrape.txt")
write_flight_rows_to_csv(flight_rows, Path("yatra_scrape.csv"))
csv_row_count = len(flight_rows)
print(f"Saved {csv_row_count} flights to yatra_scrape.csv")
page.screenshot(path="screenshot.png")

browser.close()
print("Browser closed successfully.")