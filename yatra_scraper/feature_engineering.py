import csv
import re
from datetime import date, datetime
from pathlib import Path


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
	parsed = datetime.strptime(value, "%d %b").date()
	candidate = parsed.replace(year=today.year)
	if candidate < today:
		candidate = candidate.replace(year=today.year + 1)
	return candidate


def _available_fare_classes(text: str) -> str:
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
) -> list[dict[str, str | int | bool]]:
	today = today or date.today()
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	fare_class = fare_class or _available_fare_classes(text)
	scrape_timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
	rows = []

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

		flight_date = _departure_date(departure_date_text, today)
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
		rows.append(row)

	return rows


def convert_flight_text_to_csv(input_path: Path, output_path: Path) -> int:
	text = input_path.read_text(encoding="utf-8")
	rows = parse_flight_text(text)
	write_flight_rows_to_csv(rows, output_path)
	return len(rows)


def write_flight_rows_to_csv(
	rows: list[dict[str, str | int | bool]], output_path: Path
) -> None:
	with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
		writer.writeheader()
		writer.writerows(rows)


if __name__ == "__main__":
	import argparse

	parser = argparse.ArgumentParser(description="Convert Yatra flight text to CSV.")
	parser.add_argument("input", nargs="?", type=Path, default=Path("flight_results.txt"))
	parser.add_argument("--output", type=Path)
	arguments = parser.parse_args()
	output_path = arguments.output or arguments.input.with_suffix(".csv")
	row_count = convert_flight_text_to_csv(arguments.input, output_path)
	print(f"Wrote {row_count} flight rows to {output_path}")