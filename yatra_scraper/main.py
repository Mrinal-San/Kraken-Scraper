from __future__ import annotations

import json
import random
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import date, datetime, timedelta
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
	"BLR": "Bangalore",
	"CCU": "Kolkata",
	"HYD": "Hyderabad",
	"MAA": "Chennai",
}
FARE_CLASSES = ("Economy", "Business")
DATE_OFFSETS = (0, 6, 14, 29, 44)
EXPECTED_PERMUTATION_COUNT = (
	len(AIRPORTS) * (len(AIRPORTS) - 1) * len(DATE_OFFSETS) * len(FARE_CLASSES)
)

HEADLESS_BROWSER = False
BROWSER_SLOW_MO_MS = 0
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
REQUEST_DELAY_MIN_SECONDS = 10
REQUEST_DELAY_MAX_SECONDS = 15
DEPARTURE_DATE_INPUT_SELECTOR = '[aria-label="Departure date inputbox"]'

OUTPUT_CSV_PATH = Path("Yatra_data.csv")
CHECKPOINT_PATH = Path("yatra_scraper_checkpoint.json")
PAUSE_FILE_PATH = Path("pause_scraper.pause")
PAUSE_POLL_INTERVAL_SECONDS = 0.25

USER_AGENTS = (
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
	"(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
	"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
	"(KHTML, like Gecko) Version/18.1 Safari/605.1.15",
	"Mozilla/5.0 (X11; Linux x86_64; rv:133.0) Gecko/20100101 Firefox/133.0",
)


class RetryableRequestError(RuntimeError):
	"""Raised when a failed page request should be retried."""


class ChallengeTimeoutError(RuntimeError):
	"""Raised when a CAPTCHA remains after the manual wait period."""


class PauseController:
	def __init__(self, root: tk.Tk) -> None:
		self.root = root
		self.paused = threading.Event()
		self.stopped = threading.Event()
		self.status_text = tk.StringVar(value="Running")
		self.iteration_text = tk.StringVar(value="Preparing scraper...")

		root.title("Yatra scraper")
		root.geometry("320x160")
		root.resizable(False, False)
		root.protocol("WM_DELETE_WINDOW", self.stop)

		tk.Label(root, text="Yatra flight scraper", font=("Segoe UI", 12, "bold")).pack(
			pady=(18, 4)
		)
		tk.Label(root, textvariable=self.status_text).pack()
		tk.Label(root, textvariable=self.iteration_text).pack(pady=(4, 10))
		self.pause_button = tk.Button(
			root,
			text="Pause",
			width=14,
			command=self.toggle_pause,
		)
		self.pause_button.pack()

	def toggle_pause(self) -> None:
		if self.paused.is_set():
			self.paused.clear()
			self.pause_button.config(text="Pause")
			self.status_text.set("Running")
		else:
			self.paused.set()
			self.pause_button.config(text="Play")
			self.status_text.set("Paused; waiting before the next request")

	def set_iteration(self, iteration: int, total: int, details: str) -> None:
		try:
			self.root.after(
				0,
				lambda: self.iteration_text.set(
					f"Iteration {iteration} of {total}\n{details}"
				),
			)
		except tk.TclError:
			pass

	def set_status(self, status: str) -> None:
		try:
			self.root.after(0, lambda: self.status_text.set(status))
		except tk.TclError:
			pass

	def wait_if_paused(self) -> None:
		while self.paused.is_set() or PAUSE_FILE_PATH.exists():
			if self.stopped.is_set():
				return
			self.stopped.wait(PAUSE_POLL_INTERVAL_SECONDS)

	def wait_for_delay(self, seconds: float) -> bool:
		deadline = time.monotonic() + seconds
		while time.monotonic() < deadline:
			self.wait_if_paused()
			if self.stopped.is_set():
				return False
			remaining = deadline - time.monotonic()
			self.stopped.wait(min(remaining, PAUSE_POLL_INTERVAL_SECONDS))
		return not self.stopped.is_set()

	def stop(self) -> None:
		self.stopped.set()
		self.paused.clear()
		try:
			self.root.destroy()
		except tk.TclError:
			pass


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
			for day_offset in DATE_OFFSETS:
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


def wait_for_challenge_clearance(page: Page) -> None:
	if not is_challenge_page(page):
		return

	print(
		"A Yatra/Akamai challenge was detected. Resolve it manually in the browser "
		f"within {CHALLENGE_TIMEOUT_SECONDS} seconds."
	)
	deadline = time.monotonic() + CHALLENGE_TIMEOUT_SECONDS
	while time.monotonic() < deadline:
		if not is_challenge_page(page):
			print("Challenge cleared; continuing.")
			return
		page.wait_for_timeout(CHALLENGE_POLL_INTERVAL_MS)

	raise ChallengeTimeoutError(
		"Challenge remained active after the manual resolution timeout"
	)


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
	date_input = page.locator(DEPARTURE_DATE_INPUT_SELECTOR)
	if date_input.count():
		date_input.click()
	page.locator(".react-datepicker").first.wait_for(
		state="visible",
		timeout=ELEMENT_TIMEOUT_MS,
	)

	target_month = (departure_date.year, departure_date.month)
	calendar_month = page.locator(".react-datepicker__current-month").first
	for _ in range(12):
		visible_month = datetime.strptime(
			calendar_month.inner_text().strip(),
			"%B %Y",
		)
		visible_month_value = (visible_month.year, visible_month.month)
		if visible_month_value == target_month:
			break
		if visible_month_value > target_month:
			raise RetryableRequestError(
				f"Calendar opened after target month for {departure_date.isoformat()}"
			)

		next_month = page.locator(
			'button[aria-label="Next Month"], '
			'button.react-datepicker__navigation--next, '
			'.react-datepicker__navigation--next'
		).filter(visible=True).first
		if not next_month.count():
			raise RetryableRequestError(
				f"Next-month control was not visible for {departure_date.isoformat()}"
			)
		next_month.click()
		page.wait_for_timeout(500)
	else:
		raise RetryableRequestError(
			f"Could not reach target month for {departure_date.isoformat()}"
		)

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

	calendar_day.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
	calendar_day.click()
	page.wait_for_timeout(500)
	return



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


def collect_flights_while_scrolling(
	page: Page,
	fare_class: str | None = None,
) -> list[dict]:
	flights_by_number = {}
	previous_height = None
	previous_flight_count = 0
	stable_bottom_rounds = 0

	for _ in range(MAX_SCROLL_ROUNDS):
		page.locator("body").wait_for(state="visible", timeout=PAGE_LOAD_TIMEOUT_MS)
		for row in parse_flight_text(
			page.locator("body").inner_text(),
			fare_class=fare_class,
		):
			flights_by_number.setdefault(row["flight_number"], row)

		page.evaluate(
			"""() => {
				const step = Math.max(window.innerHeight * 0.8, 400);
				window.scrollBy(0, step);
			}"""
		)
		page.wait_for_timeout(SCROLL_WAIT_MS)

		for row in parse_flight_text(
			page.locator("body").inner_text(),
			fare_class=fare_class,
		):
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

	wait_for_challenge_clearance(page)
	select_location(page, "Departure From", parameters.origin)
	select_location(page, "", parameters.destination)
	select_departure_date(page, parameters.departure_date)
	select_fare_class(page, parameters.fare_class)

	result_page = open_search_results(page)
	wait_for_challenge_clearance(result_page)
	flight_rows = collect_flights_while_scrolling(result_page, parameters.fare_class)

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


def save_checkpoint(
	start_date: date,
	next_iteration: int,
	parameters: list[SearchParameters],
) -> None:
	checkpoint = {
		"start_date": start_date.isoformat(),
		"next_iteration": next_iteration,
	}
	if next_iteration <= len(parameters):
		next_search = parameters[next_iteration - 1]
		checkpoint.update(
			{
				"next_origin": next_search.origin,
				"next_destination": next_search.destination,
				"next_departure_date": next_search.departure_date.isoformat(),
				"next_fare_class": next_search.fare_class,
			}
		)
	else:
		checkpoint["status"] = "completed"
	temporary_path = CHECKPOINT_PATH.with_suffix(".tmp")
	temporary_path.write_text(json.dumps(checkpoint), encoding="utf-8")
	temporary_path.replace(CHECKPOINT_PATH)


def load_checkpoint(
	default_start_date: date,
	total_iterations: int,
) -> tuple[date, int]:
	if not CHECKPOINT_PATH.exists():
		return default_start_date, 1

	try:
		checkpoint = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
		start_date = date.fromisoformat(checkpoint["start_date"])
		next_iteration = int(checkpoint["next_iteration"])
		if 1 <= next_iteration <= total_iterations:
			return start_date, next_iteration
		if next_iteration == total_iterations + 1:
			return start_date, next_iteration
	except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
		pass

	return default_start_date, 1


def wait_if_paused() -> None:
	if not PAUSE_FILE_PATH.exists():
		return

	print(
		f"Paused because {PAUSE_FILE_PATH} exists. "
		"Remove the file to resume."
	)
	while PAUSE_FILE_PATH.exists():
		time.sleep(PAUSE_POLL_INTERVAL_SECONDS)
	print("Pause file removed; resuming.")


def scrape_with_retries(
	browser,
	parameters: SearchParameters,
	request_number: int,
	controller: PauseController,
) -> int:
	for attempt in range(1, MAX_RETRIES + 1):
		delay = random.uniform(
			REQUEST_DELAY_MIN_SECONDS,
			REQUEST_DELAY_MAX_SECONDS,
		)
		print(
			f"Waiting {delay:.1f} seconds before request "
			f"{request_number}, attempt {attempt}."
		)
		if not controller.wait_for_delay(delay):
			return 0

		user_agent = USER_AGENTS[(request_number + attempt - 2) % len(USER_AGENTS)]
		context = browser.new_context(user_agent=user_agent)
		page = context.new_page()
		try:
			rows = scrape_permutation(page, parameters)
			return append_rows_to_csv(rows, OUTPUT_CSV_PATH)
		except ChallengeTimeoutError as error:
			print(
				f"Skipping {parameters.origin}-{parameters.destination} "
				f"{parameters.departure_date} {parameters.fare_class}: {error}"
			)
			return 0
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
			if not controller.wait_for_delay(backoff):
				return 0
		finally:
			context.close()
	return 0


def run_scraper(controller: PauseController) -> None:
	default_start_date = date.today() + timedelta(days=1)
	start_date, next_iteration = load_checkpoint(
		default_start_date,
		EXPECTED_PERMUTATION_COUNT,
	)
	parameters = generate_search_parameters(start_date)
	if next_iteration > len(parameters):
		CHECKPOINT_PATH.unlink(missing_ok=True)
		next_iteration = 1

	print(f"Generated {len(parameters)} search permutations.")
	if next_iteration > 1:
		print(f"Resuming from iteration {next_iteration}.")
	controller.set_status("Running")

	playwright = sync_playwright().start()
	browser = playwright.firefox.launch(
		headless=HEADLESS_BROWSER,
		slow_mo=BROWSER_SLOW_MO_MS,
	)
	try:
		for request_number, search_parameters in enumerate(
			parameters[next_iteration - 1:],
			start=next_iteration,
		):
			controller.set_iteration(
				request_number,
				len(parameters),
				f"{search_parameters.origin}-{search_parameters.destination} | "
				f"{search_parameters.departure_date} | {search_parameters.fare_class}",
			)
			controller.wait_if_paused()
			if controller.stopped.is_set():
				save_checkpoint(start_date, request_number, parameters)
				return
			save_checkpoint(start_date, request_number, parameters)
			rows_saved = scrape_with_retries(
				browser,
				search_parameters,
				request_number,
				controller,
			)
			if controller.stopped.is_set():
				save_checkpoint(start_date, request_number, parameters)
				return
			print(
				f"Completed {request_number}/{len(parameters)}: "
				f"{search_parameters.origin}-{search_parameters.destination}, "
				f"{search_parameters.departure_date}, "
				f"{search_parameters.fare_class}; saved {rows_saved} rows."
			)
			save_checkpoint(start_date, request_number + 1, parameters)
		if CHECKPOINT_PATH.exists():
			CHECKPOINT_PATH.unlink()
		print("All permutations completed; checkpoint cleared.")
		controller.set_status("Completed")
		controller.root.after(0, controller.root.destroy)
	except KeyboardInterrupt:
		save_checkpoint(start_date, request_number, parameters)
		print(
			f"Paused at iteration {request_number}. "
			"Run the script again to resume from this iteration."
		)
		controller.set_status("Paused; restart to resume")
	finally:
		browser.close()
		playwright.stop()
		print("Browser closed successfully.")


def run_scraper_safely(controller: PauseController) -> None:
	try:
		run_scraper(controller)
	except Exception as error:
		print(f"Scraper stopped unexpectedly: {error}")
		controller.set_status(f"Stopped: {error}")
		try:
			controller.root.after(0, controller.root.destroy)
		except tk.TclError:
			pass


def main() -> None:
	root = tk.Tk()
	controller = PauseController(root)
	worker = threading.Thread(
		target=run_scraper_safely,
		args=(controller,),
		daemon=True,
	)
	worker.start()
	root.mainloop()


if __name__ == "__main__":
	main()
