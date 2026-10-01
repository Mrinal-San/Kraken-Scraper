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

FLIGHT_NUMBER_RE = re.compile(
	r"^(?P<prefix>[A-Z0-9]{2,3})-\s*(?P<number>\d{2,4})$",
	re.IGNORECASE,
)
AIRPORT_RE = re.compile(r"^(.+?)\s*\(([A-Z]{3})\)$")
BUSINESS_AIRPORT_RE = re.compile(r"^([A-Z]{3})\s+(\d{1,2}:\d{2})$")
TIME_RE = re.compile(r"^\d{1,2}:\d{2}$")
DURATION_RE = re.compile(r"^\d+\s*h(?:\s*\d+\s*m)?$", re.IGNORECASE)
STOP_RE = re.compile(r"^(?:non\s*stop|\d+\s*stops?)$", re.IGNORECASE)
PRICE_RE = re.compile(r"(?:₹|Rs\.?\s*)([\d,]+)", re.IGNORECASE)


def parse_flight_text(
	text: str,
	departure_date: date | None = None,
	today: date | None = None,
	fare_class: str | None = None,
) -> list[dict[str, str | int | bool]]:
	today = today or date.today()
	departure_date = departure_date or today
	scrape_timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
	lines = [line.strip() for line in text.splitlines() if line.strip()]
	rows = []

	for index, flight_number in enumerate(lines):
		flight_match = FLIGHT_NUMBER_RE.fullmatch(flight_number)
		if not flight_match or index == 0:
			continue
		flight_number = (
			f"{flight_match.group('prefix')}-{flight_match.group('number')}"
		)

		following_lines = lines[index + 1:]
		airport_matches = []
		times = []
		for line in following_lines:
			if airport_match := AIRPORT_RE.fullmatch(line):
				airport_matches.append(
					(airport_match.group(1).strip(), airport_match.group(2))
				)
			elif business_match := BUSINESS_AIRPORT_RE.fullmatch(line):
				airport_matches.append(("", business_match.group(1)))
				times.append(business_match.group(2))
			elif TIME_RE.fullmatch(line):
				times.append(line)
		duration = next(
			(line for line in following_lines if DURATION_RE.fullmatch(line)),
			"",
		)
		stops_text = next(
			(line for line in following_lines if STOP_RE.fullmatch(line)),
			"",
		)
		price_match = PRICE_RE.search(" ".join(following_lines))
		if len(airport_matches) < 2 or len(times) < 2 or not duration:
			continue

		(origin_name, origin_code), (destination_name, destination_code) = (
			airport_matches[:2]
		)
		stops_match = re.search(r"\d+", stops_text)
		stop_count = 0 if stops_text and "non" in stops_text.lower() else (
			int(stops_match.group()) if stops_match else ""
		)
		row = {column: "" for column in CSV_COLUMNS}
		row.update(
			{
				"airline": lines[index - 1],
				"flight_number": flight_number,
				"fare_class": fare_class or "",
				"departure_airport": AIRPORT_NAMES.get(origin_code, origin_name),
				"departure_airport_code": origin_code,
				"arrival_airport": AIRPORT_NAMES.get(destination_code, destination_name),
				"arrival_airport_code": destination_code,
				"stop_counts": stop_count,
				"departure_date": departure_date.isoformat(),
				"departure_time": times[0],
				"arrival_time": times[1],
				"flight_duration": duration,
				"departure_hour": int(times[0].split(":", 1)[0]),
				"day_of_week": departure_date.strftime("%A"),
				"month_of_year": departure_date.month,
				"is_weekend": departure_date.weekday() >= 5,
				"scrape_date": today.isoformat(),
				"scrape_timestamp": scrape_timestamp,
				"days_until_departure": (departure_date - today).days,
				"website": "EaseMyTrip",
			}
		)
		if price_match:
			row["current_price"] = int(price_match.group(1).replace(",", ""))
			row["currency"] = "INR"
		rows.append(row)

	return rows


def write_flight_rows_to_csv(
	rows: list[dict[str, str | int | bool]], output_path: Path
) -> None:
	with output_path.open("w", newline="", encoding="utf-8-sig") as csv_file:
		writer = csv.DictWriter(csv_file, fieldnames=CSV_COLUMNS)
		writer.writeheader()
		writer.writerows(rows)
