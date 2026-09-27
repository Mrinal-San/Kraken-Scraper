from __future__ import annotations

import random
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from playwright.sync_api import Page
from playwright.sync_api import sync_playwright

if __package__:
	from .feature_engineering import parse_flight_text
else:
	from feature_engineering import parse_flight_text


YATRA_URL = "https://www.yatra.com"
AIRPORTS = ("DEL", "BOM", "BLR", "CCU", "HYD", "MAA")
AIRPORT_SEARCH_TERMS = {
	"DEL": "New Delhi",
	"BOM": "Mumbai",
	"BLR": "Bengaluru",
	"CCU": "Kolkata",
	"HYD": "Hyderabad",
	"MAA": "Chennai",
}
FARE_CLASSES = ("Economy", "Premium Economy", "Business")
DATE_COUNT = 45
EXPECTED_PERMUTATION_COUNT = 4050

HEADLESS_BROWSER = False
BROWSER_SLOW_MO_MS = 2000
PAGE_LOAD_TIMEOUT_MS = 30000
ELEMENT_TIMEOUT_MS = 15000
CHALLENGE_TIMEOUT_SECONDS = 30
CHALLENGE_POLL_INTERVAL_MS = 500
MAX_SCROLL_ROUNDS = 60
STABLE_BOTTOM_ROUNDS_REQUIRED = 3
SCROLL_WAIT_MS = 700
RESULTS_WAIT_MS = 1500
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY_MIN_SECONDS = 20
REQUEST_DELAY_MAX_SECONDS = 40

OUTPUT_CSV_PATH = Path("Yatra_data.csv")

USER_AGENTS = (
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
	"(KHTML, like Gecko) Version/18.1 Safari/605.1.15",
	"Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
)


class RetryableRequestError(RuntimeError):
	"""Raised when a page request should be retried."""


@dataclass(frozen=True)
class SearchParameters:
	origin: str
	destination: str
	departure_date: date
	fare_class: str


def generate_search_parameters(start_date: date) -> list[SearchParameters]:
	parameters = []
	for origin in AIRPORTS:
		for destination in AIRPORTS:
			if origin == destination:
				continue
			for day_offset in range(DATE_COUNT):
				for fare_class in FARE_CLASSES:
					parameters.append(
						SearchParameters(
							origin=origin,
							destination=destination,
							departure_date=start_date + timedelta(days=day_offset),
							fare_class=fare_class,
						)
					)

	if len(parameters) != EXPECTED_PERMUTATION_COUNT:
		raise RuntimeError(
			f"Expected {EXPECTED_PERMUTATION_COUNT} permutations, "
			f"generated {len(parameters)}"
		)
	return parameters


def is_challenge_page(page: Page) -> bool:
	title = page.title().lower()
	url = page.url.lower()
	return (
		any(marker in title for marker in ("challenge", "attention", "validation", "captcha"))
		or any(marker in url for marker in ("akamai", "bot", "challenge", "captcha"))
	)


def wait_for_challenge_timeout(page: Page) -> None:
	if not is_challenge_page(page):
		return

	print(
		"A Yatra/Akamai challenge was detected. "
		f"Waiting up to {CHALLENGE_TIMEOUT_SECONDS} seconds for it to clear."
	)
	deadline = time.monotonic() + CHALLENGE_TIMEOUT_SECONDS
	while time.monotonic() < deadline:
		if not is_challenge_page(page):
			print("Challenge cleared; continuing.")
			return
		page.wait_for_timeout(CHALLENGE_POLL_INTERVAL_MS)

	print("Challenge timeout reached; continuing with scrolling and parsing.")


def select_location(page: Page, field_label: str, airport_code: str) -> None:
	if field_label:
		page.locator(
			f'[role="Combobox"][aria-label^="{field_label}"]'
		).click()

	search_term = AIRPORT_SEARCH_TERMS[airport_code]
	city_input = page.locator("#input-with-icon-adornment")
	city_input.fill(search_term)
	page.get_by_text(search_term, exact=True).first.click()


def select_departure_date(page: Page, departure_date: date) -> None:
	day = departure_date.day
	if 10 <= day % 100 <= 20:
		ordinal = "th"
	else:
		ordinal = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

	date_label = (
		f"Choose {departure_date.strftime('%A, %B')} "
		f"{day}{ordinal}, {departure_date.year}"
	)
	calendar_day = page.locator(
		f'.react-datepicker__day:not(.react-datepicker__day--outside-month)'
		f'[aria-label="{date_label}"][aria-disabled="false"]'
	)

	for _ in range(12):
		if calendar_day.count():
			calendar_day.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
			calendar_day.click()
			page.wait_for_timeout(500)
			return

		next_month = page.locator(
			'button[aria-label="Next Month"], '
			'.react-datepicker__navigation--next'
		).first
		if not next_month.count():
			break
		next_month.click()

	raise RetryableRequestError(
		f"Departure date {departure_date.isoformat()} was not available"
	)


def select_fare_class(page: Page, fare_class: str) -> None:
	page.locator('[aria-label="Travellers class inputbox"]').click()
	option = page.locator(f'label[role="radio"][aria-label="{fare_class}"]')
	if not option.count():
		option = page.get_by_text(fare_class, exact=True).first
	option.click()
	page.get_by_role("button", name="Done", exact=True).click()


def open_search_results(page: Page) -> Page:
	opened_pages = []
	context = page.context

	def capture_opened_page(opened_page):
		opened_pages.append(opened_page)

	context.on("page", capture_opened_page)
	try:
		page.get_by_role("button", name="Search", exact=True).click()
		page.wait_for_timeout(RESULTS_WAIT_MS)
	finally:
		context.remove_listener("page", capture_opened_page)

	if opened_pages:
		result_page = opened_pages[-1]
		result_page.wait_for_load_state(
			"domcontentloaded", timeout=PAGE_LOAD_TIMEOUT_MS
		)
		return result_page
	return page


def collect_flights_while_scrolling(page: Page) -> list[dict]:
	flights_by_number = {}
	previous_height = None
	previous_flight_count = 0
	stable_bottom_rounds = 0

	for _ in range(MAX_SCROLL_ROUNDS):
		page.locator("body").wait_for(state="visible", timeout=PAGE_LOAD_TIMEOUT_MS)
		for row in parse_flight_text(page.locator("body").inner_text()):
			flights_by_number.setdefault(row["flight_number"], row)

		page.evaluate(
			"""() => {
				const step = Math.max(window.innerHeight * 0.8, 400);
				window.scrollBy(0, step);
			}"""
		)
		page.wait_for_timeout(SCROLL_WAIT_MS)

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

		if stable_bottom_rounds >= STABLE_BOTTOM_ROUNDS_REQUIRED:
			break
		previous_height = metrics["height"]
		previous_flight_count = flight_count

	return list(flights_by_number.values())


def scrape_permutation(page: Page, parameters: SearchParameters) -> list[dict]:
	response = page.goto(
		YATRA_URL,
		wait_until="domcontentloaded",
		timeout=PAGE_LOAD_TIMEOUT_MS,
	)
	if response and response.status in (403, 429):
		raise RetryableRequestError(f"Yatra returned HTTP {response.status}")

	wait_for_challenge_timeout(page)
	select_location(page, "Departure From", parameters.origin)
	select_location(page, "", parameters.destination)
	select_departure_date(page, parameters.departure_date)
	select_fare_class(page, parameters.fare_class)

	result_page = open_search_results(page)
	wait_for_challenge_timeout(result_page)
	flight_rows = collect_flights_while_scrolling(result_page)

	for row in flight_rows:
		row.update(
			{
				"requested_origin": parameters.origin,
				"requested_destination": parameters.destination,
				"requested_departure_date": parameters.departure_date.isoformat(),
				"requested_fare_class": parameters.fare_class,
			}
		)
	return flight_rows


def append_rows_to_csv(rows: list[dict], output_path: Path) -> int:
	if not rows:
		return 0

	data_frame = pd.DataFrame(rows)
	write_header = not output_path.exists() or output_path.stat().st_size == 0
	data_frame.to_csv(
		output_path,
		mode="a",
		header=write_header,
		index=False,
		encoding="utf-8-sig",
	)
	return len(data_frame)


def scrape_with_retries(browser, parameters: SearchParameters, request_number: int) -> int:
	for attempt in range(1, MAX_RETRIES + 1):
		delay = random.uniform(
			REQUEST_DELAY_MIN_SECONDS,
			REQUEST_DELAY_MAX_SECONDS,
		)
		print(f"Waiting {delay:.1f} seconds before request {request_number}, attempt {attempt}.")
		time.sleep(delay)

		user_agent = USER_AGENTS[(request_number + attempt - 2) % len(USER_AGENTS)]
		context = browser.new_context(user_agent=user_agent)
		page = context.new_page()
		try:
			rows = scrape_permutation(page, parameters)
			return append_rows_to_csv(rows, OUTPUT_CSV_PATH)
		except Exception as error:
			if attempt == MAX_RETRIES:
				print(
					f"Failed {parameters.origin}-{parameters.destination} "
					f"{parameters.departure_date} {parameters.fare_class} "
					f"after {MAX_RETRIES} attempts: {error}"
				)
				return 0
			backoff = RETRY_BACKOFF_SECONDS * (2 ** (attempt - 1))
			print(f"Attempt {attempt} failed; retrying in {backoff} seconds: {error}")
			time.sleep(backoff)
		finally:
			context.close()
	return 0


def main() -> None:
	parameters = generate_search_parameters(date.today() + timedelta(days=1))
	print(f"Generated {len(parameters)} search permutations.")

	playwright = sync_playwright().start()
	browser = playwright.firefox.launch(
		headless=HEADLESS_BROWSER,
		slow_mo=BROWSER_SLOW_MO_MS,
	)
	try:
		for request_number, search_parameters in enumerate(parameters, start=1):
			rows_saved = scrape_with_retries(
				browser,
				search_parameters,
				request_number,
			)
			print(
				f"Completed {request_number}/{len(parameters)}: "
				f"{search_parameters.origin}-{search_parameters.destination}, "
				f"{search_parameters.departure_date}, "
				f"{search_parameters.fare_class}; saved {rows_saved} rows."
			)
	finally:
		browser.close()
		playwright.stop()
		print("Browser closed successfully.")


if __name__ == "__main__":
	main()
from playwright.sync_api import sync_playwright
from datetime import date, timedelta
import time
from pathlib import Path

if __package__:
	from .feature_engineering import parse_flight_text, write_flight_rows_to_csv
else:
	from feature_engineering import parse_flight_text, write_flight_rows_to_csv


YATRA_URL = "https://www.yatra.com"
DEPARTURE_CITY = "Mumbai"
ARRIVAL_CITY = "New Delhi"
DEPARTURE_DAYS_AHEAD = 1
HEADLESS_BROWSER = False
BROWSER_SLOW_MO_MS = 2000
PAGE_LOAD_TIMEOUT_MS = 6000
CHALLENGE_TIMEOUT_SECONDS = 300
CHALLENGE_POLL_INTERVAL_MS = 500
MAX_SCROLL_ROUNDS = 60
STABLE_BOTTOM_ROUNDS_REQUIRED = 3
OUTPUT_TEXT_PATH = Path("yatra_scrape.txt")
OUTPUT_CSV_PATH = Path("yatra_scrape.csv")
SCREENSHOT_PATH = Path("screenshot.png")


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
	deadline = time.monotonic() + CHALLENGE_TIMEOUT_SECONDS
	while time.monotonic() < deadline:
		if not is_challenge_page(page):
			print(f"Challenge cleared. Continuing at {page.url}")
			return
		page.wait_for_timeout(CHALLENGE_POLL_INTERVAL_MS)

	raise TimeoutError("The challenge was not cleared within 5 minutes.")


def collect_flights_while_scrolling(page):
	flights_by_number = {}
	previous_height = None
	previous_flight_count = 0
	stable_bottom_rounds = 0

	for _ in range(MAX_SCROLL_ROUNDS):
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

		if stable_bottom_rounds >= STABLE_BOTTOM_ROUNDS_REQUIRED:
			break
		previous_height = metrics["height"]
		previous_flight_count = flight_count

	return list(flights_by_number.values())


def select_location(page, field_label, city):
	if field_label:
		page.locator(
			f'[role="Combobox"][aria-label^="{field_label}"]'
		).click()

	city_input = page.locator("#input-with-icon-adornment")
	city_input.fill(city)
	page.get_by_text(city, exact=True).first.click()
	print(f"{field_label or 'Destination'} changed to {city} successfully!")


def select_departure_date(page, departure_date):
	day = departure_date.day
	if 10 <= day % 100 <= 20:
		ordinal = "th"
	else:
		ordinal = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")

	date_label = (
		f"Choose {departure_date.strftime('%A, %B')} "
		f"{day}{ordinal}, {departure_date.year}"
	)
	calendar_day = page.locator(
		f'.react-datepicker__day:not(.react-datepicker__day--outside-month)'
		f'[aria-label="{date_label}"][aria-disabled="false"]'
	)
	calendar_day.wait_for(state="visible", timeout=10000)
	calendar_day.click()
	page.wait_for_timeout(500)


def select_traveller_class(page):
	page.locator('[aria-label="Travellers class inputbox"]').click()
	premium_economy = page.locator('label[role="radio"][aria-label="Premium Economy"]')
	premium_economy.focus()
	premium_economy.press("Space")
	if not page.locator('input[type="radio"][value="Special"]').is_checked():
		raise RuntimeError("Premium Economy was not selected")
	page.get_by_role("button", name="Done", exact=True).click()


def open_search_results(page):
	opened_pages = []

	def capture_opened_page(opened_page):
		opened_pages.append(opened_page)

	context = page.context
	context.on("page", capture_opened_page)
	try:
		page.get_by_role("button", name="Search", exact=True).click()
		page.wait_for_timeout(1500)
	finally:
		context.remove_listener("page", capture_opened_page)

	if opened_pages:
		page = opened_pages[-1]
		page.wait_for_load_state("domcontentloaded", timeout=30000)
		print(f"Results opened in a new tab: {page.url}")

	return page


def save_results(page, output_text_path, output_csv_path, screenshot_path):
	print(page.title())
	page.wait_for_timeout(1500)
	print("Scrolling through flight results and collecting loaded flights...")
	flight_rows = collect_flights_while_scrolling(page)
	output_text_path.write_text(page.locator("body").inner_text(), encoding="utf-8")
	print(f"Page text saved to {output_text_path}")
	write_flight_rows_to_csv(flight_rows, output_csv_path)
	print(f"Saved {len(flight_rows)} flights to {output_csv_path}")
	page.screenshot(path=str(screenshot_path))


def main():
	playwright = sync_playwright().start()
	browser = playwright.firefox.launch(
		headless=HEADLESS_BROWSER,
		slow_mo=BROWSER_SLOW_MO_MS,
	)
	try:
		page = browser.new_page()
		page.goto(
			YATRA_URL,
			wait_until="domcontentloaded",
			timeout=PAGE_LOAD_TIMEOUT_MS,
		)
		pause_for_challenge_if_needed(page)

		select_location(page, "Departure From", DEPARTURE_CITY)
		select_location(page, "", ARRIVAL_CITY)
		select_departure_date(
			page,
			date.today() + timedelta(days=DEPARTURE_DAYS_AHEAD),
		)
		select_traveller_class(page)

		page = open_search_results(page)
		pause_for_challenge_if_needed(page)
		save_results(
			page,
			OUTPUT_TEXT_PATH,
			OUTPUT_CSV_PATH,
			SCREENSHOT_PATH,
		)
	finally:
		browser.close()
		playwright.stop()
		print("Browser closed successfully.")


if __name__ == "__main__":
	main()