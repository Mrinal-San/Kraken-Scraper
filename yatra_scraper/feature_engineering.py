import csv
import re
import tempfile
from datetime import date, datetime
from pathlib import Path
from typing import Iterable, Iterator


CSV_COLUMNS = [
	"airline",
	"flight_number",
	"fare_class",
	"departure_airport",
	"departure_airport_code",
	"arrival_airport",
	"arrival_airport_code",
	"stop_counts",
	"departure_date",
	"departure_time",
	"arrival_time",
	"flight_duration",
	"departure_hour",
	"day_of_week",
	"month_of_year",
	"is_weekend",
	"holiday_season",
	"scrape_date",
	"scrape_timestamp",
	"days_until_departure",
	"website",
	"current_price",
	"currency",
]
UNIQUE_ROW_KEY_FIELDS = (
	"website",
	"flight_number",
	"departure_date",
	"departure_time",
	"scrape_timestamp",
)

AIRPORT_NAMES = {
	"DEL": "Indira Gandhi International Airport",
	"BOM": "Chhatrapati Shivaji Maharaj International Airport Mumbai",
	"BLR": "Kempegowda International Airport Bengaluru",
	"HYD": "Rajiv Gandhi International Airport",
	"CCU": "Netaji Subhash Chandra Bose International Airport",
	"MAA": "Chennai International Airport",
}

FLIGHT_NUMBER_RE = re.compile(r"^[A-Z0-9]{2,3}-\d{2,4}$", re.IGNORECASE)
AIRPORT_RE = re.compile(r"^(.+?)\(([A-Z]{3})\)$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DATE_RE = re.compile(r"^\d{1,2}\s+[A-Za-z]{3}$")
DURATION_RE = re.compile(r"^\d+\s*h(?:\s*\d+\s*m)?$", re.IGNORECASE)
PRICE_RE = re.compile(r"^(₹|Rs\.?\s*)([\d,]+)$", re.IGNORECASE)
FARE_CLASS_RE = re.compile(
	r"\b(Premium Economy|Economy|Business)\b",
	re.IGNORECASE,
)


def _departure_date(value: str, today: date) -> date:
	"""Resolve a day/month label to its next matching calendar date."""
	parsed = datetime.strptime(value, "%d %b").date()
	candidate = parsed.replace(year=today.year)
	if candidate < today:
		candidate = candidate.replace(year=today.year + 1)
	return candidate


def _available_fare_classes(text: str) -> str:
	"""Extract distinct fare class labels present in a result-page text."""
	fare_classes = []
	for match in FARE_CLASS_RE.finditer(text):
		fare_class = match.group(1).title()
		if fare_class not in fare_classes:
			fare_classes.append(fare_class)
	return ", ".join(fare_classes)


def parse_flight_text(
	text: str,
	today: date | None = None,
	fare_class: str | None = None,
	departure_date: date | None = None,
	scrape_timestamp: str | None = None,
) -> Iterator[dict[str, str | int | bool]]:
	"""Yield normalized flight rows found in one Yatra results page."""
	today = today or date.today()
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	fare_class = fare_class or _available_fare_classes(text)
	scrape_timestamp = scrape_timestamp or datetime.now().astimezone().isoformat(
		timespec="seconds"
	)

	for index, flight_number in enumerate(lines):
		if not FLIGHT_NUMBER_RE.fullmatch(flight_number) or index == 0:
			continue
		if index + 10 >= len(lines):
			continue

		airline = lines[index - 1]
		origin_match = AIRPORT_RE.fullmatch(lines[index + 2])
		destination_match = AIRPORT_RE.fullmatch(lines[index + 8])
		departure_time = lines[index + 3]
		departure_date_text = lines[index + 4]
		flight_duration = lines[index + 6]
		stops_text = lines[index + 7]
		arrival_time = lines[index + 9]
		arrival_date_text = lines[index + 10]

		price_index = index + 12
		price_match = PRICE_RE.fullmatch(lines[price_index]) if price_index < len(lines) else None

		if not (
			origin_match
			and destination_match
			and TIME_RE.fullmatch(departure_time)
			and DATE_RE.fullmatch(departure_date_text)
			and DURATION_RE.fullmatch(flight_duration)
			and TIME_RE.fullmatch(arrival_time)
			and DATE_RE.fullmatch(arrival_date_text)
		):
			continue

		flight_date = departure_date or _departure_date(departure_date_text, today)
		stops_match = re.search(r"\d+", stops_text)
		stop_count = 0 if "non stop" in stops_text.lower() else (
			int(stops_match.group()) if stops_match else ""
		)
		row = {column: "" for column in CSV_COLUMNS}
		row.update(
			{
				"airline": airline,
				"flight_number": flight_number,
				"fare_class": fare_class,
				"departure_airport": AIRPORT_NAMES.get(
					origin_match.group(2), origin_match.group(1).strip()
				),
				"departure_airport_code": origin_match.group(2),
				"arrival_airport": AIRPORT_NAMES.get(
					destination_match.group(2), destination_match.group(1).strip()
				),
				"arrival_airport_code": destination_match.group(2),
				"stop_counts": stop_count,
				"departure_date": flight_date.isoformat(),
				"departure_time": departure_time,
				"arrival_time": arrival_time,
				"flight_duration": flight_duration,
				"departure_hour": int(departure_time.split(":", 1)[0]),
				"day_of_week": flight_date.strftime("%A"),
				"month_of_year": flight_date.month,
				"is_weekend": flight_date.weekday() >= 5,
				"scrape_date": today.isoformat(),
				"scrape_timestamp": scrape_timestamp,
				"days_until_departure": (flight_date - today).days,
				"website": "Yatra",
			}
		)
		if price_match:
			row["current_price"] = int(price_match.group(2).replace(",", ""))
			row["currency"] = "INR"
		yield row


def convert_flight_text_to_csv(input_path: Path, output_path: Path) -> int:
	"""Convert a saved Yatra results-text file to a normalized CSV."""
	text = input_path.read_text(encoding="utf-8")
	return write_flight_rows_to_csv(parse_flight_text(text), output_path)


def write_flight_rows_to_csv(
	rows: Iterable[dict[str, str | int | bool]], output_path: Path
) -> int:
	"""Write a normalized CSV, replacing any existing output file."""
	output_path.parent.mkdir(parents=True, exist_ok=True)
	row_count = 0
	with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
		writer.writeheader()
		for row in rows:
			writer.writerow(row)
			row_count += 1
	return row_count


def append_flight_rows_to_csv(
	rows: Iterable[dict[str, str | int | bool]], output_path: Path
) -> int:
	"""Stage and append unique rows without materializing a full search in memory."""
	output_path.parent.mkdir(parents=True, exist_ok=True)
	row_count = 0
	seen_keys: set[tuple[str, ...]] = set()
	with tempfile.TemporaryFile(mode="w+t", newline="", encoding="utf-8") as staged_file:
		staged_writer = csv.DictWriter(staged_file, fieldnames=CSV_COLUMNS)
		staged_writer.writeheader()
		for row in rows:
			key = tuple(str(row.get(field, "")) for field in UNIQUE_ROW_KEY_FIELDS)
			if not all(key) or key in seen_keys:
				continue
			staged_writer.writerow(row)
			seen_keys.add(key)
			row_count += 1

		if not row_count:
			return 0

		staged_file.seek(0)
		write_header = not output_path.exists() or output_path.stat().st_size == 0
		with output_path.open("a", newline="", encoding="utf-8-sig") as csv_file:
			reader = csv.DictReader(staged_file)
			writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
			if write_header:
				writer.writeheader()
			writer.writerows(reader)
	return row_count


def process_raw_flight_texts_to_csv(
	raw_texts: Iterable[str],
	output_path: Path,
	*,
	fare_class: str,
	scrape_timestamp: str,
	departure_date: date | None = None,
	origin: str | None = None,
	destination: str | None = None,
) -> int:
	"""Parse raw result-page text and append unique flight rows incrementally."""
	def iter_rows() -> Iterator[dict[str, str | int | bool]]:
		"""Yield filtered, locally unique rows from the supplied page texts."""
		for raw_text in raw_texts:
			for row in parse_flight_text(
				raw_text,
				fare_class=fare_class,
				departure_date=departure_date,
				scrape_timestamp=scrape_timestamp,
			):
				if origin and row["departure_airport_code"] != origin:
					continue
				if destination and row["arrival_airport_code"] != destination:
					continue
				yield row

	return append_flight_rows_to_csv(iter_rows(), output_path)


if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser(description="Convert Yatra flight text to CSV.")
	parser.add_argument("input", nargs="?", type=Path, default=Path("flight_results.txt"))
	parser.add_argument("--output", type=Path)
	arguments = parser.parse_args()
	output_path = arguments.output or arguments.input.with_suffix(".csv")
	row_count = convert_flight_text_to_csv(arguments.input, output_path)
	print(f"Wrote {row_count} flight rows to {output_path}")