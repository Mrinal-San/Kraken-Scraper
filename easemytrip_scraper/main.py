from __future__ import annotations

import json
import random
import threading
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from typing import Iterator

from playwright.sync_api import Page, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import Browser
from playwright.sync_api import sync_playwright

if __package__:
	from .feature_engineering import process_raw_flight_texts_to_csv
else:
	from feature_engineering import process_raw_flight_texts_to_csv


EASEMYTRIP_URL = "https://www.easemytrip.com/"
AIRPORTS = ("DEL", "BOM", "BLR", "CCU", "HYD", "MAA")
AIRPORT_OPTION_LABELS = {
	"DEL": "Delhi(DEL)",
	"BOM": "Mumbai(BOM)",
	"BLR": "Bangalore(BLR)",
	"CCU": "Kolkata(CCU)",
	"HYD": "Hyderabad(HYD)",
	"MAA": "Chennai(MAA)",
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
MAX_SCROLL_ROUNDS = 120
STABLE_BOTTOM_ROUNDS_REQUIRED = 3
SCROLL_STEP_VIEWPORTS = 1.5
SCROLL_WAIT_MS = 30
BOTTOM_SETTLE_WAIT_MS = 500
MAX_RETRIES = 3
RETRY_BACKOFF_SECONDS = 5
REQUEST_DELAY_MIN_SECONDS = 10
REQUEST_DELAY_MAX_SECONDS = 15
DEPARTURE_DATE_INPUT_SELECTOR = "#ddate"
BODY_SELECTOR = "body"
FLIGHT_CARD_SELECTOR = ".nw_listing_bx, .ptclr_lbx.bscls-card"
RESULTS_SEARCH_BUTTON_NAME = "Search"
NO_FLIGHTS_MARKERS = ("no flights found", "no flight available")
CHALLENGE_TITLE_MARKERS = ("challenge", "attention", "validation", "captcha")
CHALLENGE_URL_MARKERS = ("akamai", "bot", "challenge", "captcha")
CHALLENGE_BODY_MARKERS = ("verify you are human", "unusual traffic", "captcha")
FROM_FIELD_SELECTOR = "#FromSector_show"
FROM_SUGGESTIONS_SELECTOR = "#fromautoFill"
FROM_VALUE_SELECTOR = "#FromSector"
TO_FIELD_SELECTOR = "#Editbox13_show"
TO_SUGGESTIONS_SELECTOR = "#toautoFill"
TO_VALUE_SELECTOR = "#Editbox13"
CALENDAR_MONTHS_SELECTOR = ".box1 .month2"
CALENDAR_DAY_SELECTOR_TEMPLATE = 'li[id$="_{date_value}"]'
NEXT_MONTH_SELECTOR = "#img2Nex"
FARE_CLASS_INPUT_SELECTOR = "#trvlr_colm"
FARE_CLASS_DROPDOWN_SELECTOR = "#myDropdown_n"
FARE_CLASS_OPTION_SELECTORS = {"Economy": "#lbEconomy", "Business": "#lbBusiness"}
FARE_CLASS_CLOSE_SELECTOR = "#myFunction4"
FARE_CLASS_VALUE_SELECTOR = "#pcalss"
RESULTS_PAGE_URL_GLOB = "**/flight-search/listing**"
PAGE_LOAD_STATE = "domcontentloaded"
SCRAPER_WINDOW_TITLE = "EaseMyTrip scraper"

OUTPUT_CSV_PATH = Path("easemytrip_scraper/Easemytrip_data.csv")
CHECKPOINT_PATH = Path("easemytrip_scraper/checkpoint.json")
PAUSE_FILE_PATH = Path("easemytrip_scraper/pause_scraper.pause")
PAUSE_POLL_INTERVAL_SECONDS = 0.25
CHECKPOINT_RETRY_DELAY_SECONDS = 60

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
		"""Create the pause/stop controls for a scraper run."""
		self.root = root
		self.paused = threading.Event()
		self.stopped = threading.Event()
		self.status_text = tk.StringVar(value="Running")
		self.iteration_text = tk.StringVar(value="Preparing scraper...")

		root.title(SCRAPER_WINDOW_TITLE)
		root.geometry("320x160")
		root.resizable(False, False)
		root.protocol("WM_DELETE_WINDOW", self.stop)

		tk.Label(root, text="EaseMyTrip flight scraper", font=("Segoe UI", 12, "bold")).pack(
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
		"""Toggle the worker's pause event and update the button state."""
		if self.paused.is_set():
			self.paused.clear()
			self.pause_button.config(text="Pause")
			self.status_text.set("Running")
		else:
			self.paused.set()
			self.pause_button.config(text="Play")
			self.status_text.set("Paused; waiting before the next request")

	def set_iteration(self, iteration: int, total: int, details: str) -> None:
		"""Show the current search on the Tk event loop."""
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
		"""Update the status label on the Tk event loop."""
		try:
			self.root.after(0, lambda: self.status_text.set(status))
		except tk.TclError:
			pass

	def wait_if_paused(self) -> None:
		"""Block the worker while paused or while the pause file exists."""
		while self.paused.is_set() or PAUSE_FILE_PATH.exists():
			if self.stopped.is_set():
				return
			self.stopped.wait(PAUSE_POLL_INTERVAL_SECONDS)

	def wait_for_delay(self, seconds: float) -> bool:
		"""Wait interruptibly, returning false after the user stops the run."""
		deadline = time.monotonic() + seconds
		while time.monotonic() < deadline:
			self.wait_if_paused()
			if self.stopped.is_set():
				return False
			remaining = deadline - time.monotonic()
			self.stopped.wait(min(remaining, PAUSE_POLL_INTERVAL_SECONDS))
		return not self.stopped.is_set()

	def stop(self) -> None:
		"""Signal the worker to stop and close the control window."""
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
	"""Build the bounded route, date, and fare-class search schedule."""
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
	"""Return whether the current EaseMyTrip page appears to be a challenge."""
	title = page.title().lower()
	url = page.url.lower()
	body_text = page.locator("body").inner_text().lower()
	return (
		any(marker in title for marker in CHALLENGE_TITLE_MARKERS)
		or any(marker in url for marker in CHALLENGE_URL_MARKERS)
		or any(
			marker in body_text
			for marker in CHALLENGE_BODY_MARKERS
		)
	)


def wait_for_challenge_clearance(page: Page) -> None:
	"""Wait briefly for a user to clear a visible browser challenge."""
	if not is_challenge_page(page):
		return

	print(
		"An EaseMyTrip challenge was detected. Resolve it manually in the browser "
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


def select_location(
	page: Page,
	field_selector: str,
	suggestions_selector: str,
	selected_value_selector: str,
	airport_code: str,
) -> None:
	"""Select and verify one airport in an EaseMyTrip location field."""
	field = page.locator(field_selector)
	option = page.locator(suggestions_selector).get_by_text(
		AIRPORT_OPTION_LABELS[airport_code],
		exact=True,
	)
	if not option.is_visible():
		field.click()
	option.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
	option.click()

	selected_value = page.locator(selected_value_selector).input_value()
	if not selected_value.startswith(f"{airport_code}-"):
		raise RetryableRequestError(
			f"EaseMyTrip did not select airport {airport_code} in {field_selector}"
		)


def select_departure_date(page: Page, departure_date: date) -> None:
	"""Select and verify a departure date in the EaseMyTrip calendar."""
	date_input = page.locator(DEPARTURE_DATE_INPUT_SELECTOR)
	calendar_months = page.locator(CALENDAR_MONTHS_SELECTOR).filter(visible=True)
	if not calendar_months.count():
		date_input.click(force=True)
	page.locator(CALENDAR_MONTHS_SELECTOR).filter(visible=True).first.wait_for(
		state="visible",
		timeout=ELEMENT_TIMEOUT_MS,
	)

	date_value = departure_date.strftime("%d/%m/%Y")
	calendar_day = page.locator(
		CALENDAR_DAY_SELECTOR_TEMPLATE.format(date_value=date_value)
	).filter(visible=True).first
	for _ in range(12):
		if calendar_day.count():
			calendar_day.click(force=True)
			if date_input.input_value() != date_value:
				raise RetryableRequestError(
					f"EaseMyTrip did not select departure date {date_value}"
				)
			return

		next_month = page.locator(NEXT_MONTH_SELECTOR).filter(visible=True).first
		if not next_month.count():
			break
		next_month.click(force=True)
		page.wait_for_timeout(250)

	raise RetryableRequestError(
		f"Could not select departure date {departure_date.isoformat()}"
	)



def select_fare_class(page: Page, fare_class: str) -> None:
	"""Select and verify the requested EaseMyTrip cabin class."""
	page.locator(FARE_CLASS_INPUT_SELECTOR).click()
	dropdown = page.locator(FARE_CLASS_DROPDOWN_SELECTOR)
	dropdown.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
	option_selector = FARE_CLASS_OPTION_SELECTORS.get(fare_class)
	if option_selector is None:
		raise ValueError(f"Unsupported EaseMyTrip fare class: {fare_class}")
	page.locator(option_selector).click()
	if dropdown.is_visible():
		page.locator(FARE_CLASS_CLOSE_SELECTOR).click()
	selected_class = page.locator(FARE_CLASS_VALUE_SELECTOR).inner_text().strip()
	if selected_class.casefold() != fare_class.casefold():
		raise RetryableRequestError(
			f"EaseMyTrip selected {selected_class!r}, expected {fare_class!r}"
		)


def open_search_results(page: Page) -> Page:
	"""Submit a search and wait for the EaseMyTrip results page."""
	page.get_by_role("button", name=RESULTS_SEARCH_BUTTON_NAME, exact=True).click()
	page.wait_for_url(RESULTS_PAGE_URL_GLOB, timeout=PAGE_LOAD_TIMEOUT_MS)
	page.wait_for_load_state(PAGE_LOAD_STATE, timeout=PAGE_LOAD_TIMEOUT_MS)
	return page


def collect_flight_texts_while_scrolling(page: Page) -> Iterator[str]:
	"""Yield visible card text while EaseMyTrip lazily loads more results."""
	previous_height = None
	previous_card_count = 0
	previous_card_signature = b""
	stable_bottom_rounds = 0
	cards = page.locator(FLIGHT_CARD_SELECTOR)
	try:
		cards.first.wait_for(state="visible", timeout=ELEMENT_TIMEOUT_MS)
	except PlaywrightTimeoutError:
		body_text = page.locator(BODY_SELECTOR).inner_text().lower()
		if any(marker in body_text for marker in NO_FLIGHTS_MARKERS):
			return
		raise RetryableRequestError("EaseMyTrip flight result cards did not load")

	card_texts = cards.all_inner_texts()
	for card_text in card_texts:
		yield card_text
	previous_card_count = len(card_texts)
	previous_card_signature = sha256("\0".join(card_texts).encode("utf-8")).digest()
	previous_metrics = page.evaluate(
		"""() => ({ height: document.documentElement.scrollHeight })"""
	)
	previous_height = previous_metrics["height"]

	for _ in range(MAX_SCROLL_ROUNDS):
		page.evaluate(
			f"""() => {{
				const step = Math.max(window.innerHeight * {SCROLL_STEP_VIEWPORTS}, 600);
				window.scrollBy(0, step);
			}}"""
		)
		page.wait_for_timeout(SCROLL_WAIT_MS)
		card_texts = cards.all_inner_texts()
		for card_text in card_texts:
			yield card_text
		card_count = len(card_texts)
		card_signature = sha256("\0".join(card_texts).encode("utf-8")).digest()

		metrics = page.evaluate(
			"""() => ({
				height: document.documentElement.scrollHeight,
				y: window.scrollY,
				viewport: window.innerHeight
			})"""
		)
		at_bottom = metrics["y"] + metrics["viewport"] >= metrics["height"] - 4
		if (
			at_bottom
			and metrics["height"] == previous_height
			and card_count == previous_card_count
			and card_signature == previous_card_signature
		):
			page.wait_for_timeout(BOTTOM_SETTLE_WAIT_MS)
			card_texts = cards.all_inner_texts()
			for card_text in card_texts:
				yield card_text
			card_count = len(card_texts)
			card_signature = sha256("\0".join(card_texts).encode("utf-8")).digest()
			metrics = page.evaluate(
				"""() => ({
					height: document.documentElement.scrollHeight,
					y: window.scrollY,
					viewport: window.innerHeight
				})"""
			)
			at_bottom = metrics["y"] + metrics["viewport"] >= metrics["height"] - 4
			if (
				at_bottom
				and metrics["height"] == previous_height
				and card_count == previous_card_count
				and card_signature == previous_card_signature
			):
				stable_bottom_rounds += 1
			else:
				stable_bottom_rounds = 0
		else:
			stable_bottom_rounds = 0

		if stable_bottom_rounds >= STABLE_BOTTOM_ROUNDS_REQUIRED:
			break
		previous_height = metrics["height"]
		previous_card_count = card_count
		previous_card_signature = card_signature


def scrape_permutation(page: Page, parameters: SearchParameters) -> Iterator[str]:
	"""Yield raw flight-card text for one EaseMyTrip search."""
	response = page.goto(
		EASEMYTRIP_URL,
		wait_until=PAGE_LOAD_STATE,
		timeout=PAGE_LOAD_TIMEOUT_MS,
	)
	if response and response.status in (403, 429):
		raise RetryableRequestError(f"EaseMyTrip returned HTTP {response.status}")

	wait_for_challenge_clearance(page)
	select_location(
		page,
		FROM_FIELD_SELECTOR,
		FROM_SUGGESTIONS_SELECTOR,
		FROM_VALUE_SELECTOR,
		parameters.origin,
	)
	select_location(
		page,
		TO_FIELD_SELECTOR,
		TO_SUGGESTIONS_SELECTOR,
		TO_VALUE_SELECTOR,
		parameters.destination,
	)
	select_departure_date(page, parameters.departure_date)
	select_fare_class(page, parameters.fare_class)

	result_page = open_search_results(page)
	wait_for_challenge_clearance(result_page)
	yield from collect_flight_texts_while_scrolling(result_page)


def save_checkpoint(
	start_date: date,
	next_iteration: int,
	parameters: list[SearchParameters],
) -> None:
	"""Atomically save the next search position for restart recovery."""
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


def is_checkpoint_access_denied(error: OSError) -> bool:
	"""Identify the Windows sharing violation for checkpoint replacement."""
	return (
		getattr(error, "winerror", None) == 5
		and error.filename is not None
		and error.filename2 is not None
		and Path(error.filename).name == "checkpoint.tmp"
		and Path(error.filename2).name == "checkpoint.json"
	)


def load_checkpoint(
	default_start_date: date,
	total_iterations: int,
) -> tuple[date, int]:
	"""Load a validated start date and next iteration from disk."""
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


def scrape_with_retries(
	browser: Browser,
	parameters: SearchParameters,
	request_number: int,
	controller: PauseController,
) -> int:
	"""Retry one search and persist its completed results."""
	scrape_timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
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
			return process_raw_flight_texts_to_csv(
				scrape_permutation(page, parameters),
				OUTPUT_CSV_PATH,
				fare_class=parameters.fare_class,
				scrape_timestamp=scrape_timestamp,
				departure_date=parameters.departure_date,
				origin=parameters.origin,
				destination=parameters.destination,
			)
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
	"""Run the scheduled EaseMyTrip searches with checkpoint recovery."""
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
	"""Restart after checkpoint sharing errors and report fatal worker errors."""
	while not controller.stopped.is_set():
		try:
			run_scraper(controller)
			return
		except Exception as error:
			if is_checkpoint_access_denied(error):
				print(
					"Windows denied access while replacing the checkpoint. "
					f"Restarting the scraper in {CHECKPOINT_RETRY_DELAY_SECONDS} seconds."
				)
				controller.set_status("Checkpoint access denied; retrying in 60 seconds")
				if controller.wait_for_delay(CHECKPOINT_RETRY_DELAY_SECONDS):
					continue
				return
			print(f"Scraper stopped unexpectedly: {error}")
			controller.set_status(f"Stopped: {error}")
			try:
				controller.root.after(0, controller.root.destroy)
			except tk.TclError:
				pass
			return


def main() -> None:
	"""Start the Tk controls and run scraping on a background thread."""
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
