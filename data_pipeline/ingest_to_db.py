from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
from datetime import date, datetime, time, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Iterator, Sequence

import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values
from psycopg2.pool import SimpleConnectionPool


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV_PATHS = (
	PROJECT_ROOT / "yatra_scraper" / "Yatra_data.csv",
	PROJECT_ROOT / "easemytrip_scraper" / "Easemytrip_data.csv",
)
TABLE_NAME = "flight_fares"
UNIQUE_CONSTRAINT_NAME = "uq_flight_fares_identity"
MIN_POOL_SIZE = 1
MAX_POOL_SIZE = 3
DEFAULT_BATCH_SIZE = 500

CSV_COLUMNS = (
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
)
REQUIRED_COLUMNS = (
	"website",
	"flight_number",
	"departure_date",
	"departure_time",
	"scrape_timestamp",
)
STRING_COLUMNS = frozenset(
	{
		"airline",
		"flight_number",
		"fare_class",
		"departure_airport",
		"departure_airport_code",
		"arrival_airport",
		"arrival_airport_code",
		"flight_duration",
		"day_of_week",
		"holiday_season",
		"website",
		"currency",
	}
)
INTEGER_COLUMNS = frozenset(
	{"stop_counts", "departure_hour", "month_of_year", "days_until_departure"}
)
DATE_COLUMNS = frozenset({"departure_date", "scrape_date"})
TIME_COLUMNS = frozenset({"departure_time", "arrival_time"})
BOOLEAN_COLUMNS = frozenset({"is_weekend"})

COMMON_HEADER_ALIASES = {
	"airline": "airline",
	"carrier": "airline",
	"flightnumber": "flight_number",
	"flightno": "flight_number",
	"flightnum": "flight_number",
	"fareclass": "fare_class",
	"departureairport": "departure_airport",
	"departureairportname": "departure_airport",
	"departureairportcode": "departure_airport_code",
	"origin": "departure_airport",
	"origincode": "departure_airport_code",
	"arrivalairport": "arrival_airport",
	"arrivalairportname": "arrival_airport",
	"arrivalairportcode": "arrival_airport_code",
	"destination": "arrival_airport",
	"destinationcode": "arrival_airport_code",
	"stopcounts": "stop_counts",
	"stops": "stop_counts",
	"departuredate": "departure_date",
	"departuretime": "departure_time",
	"arrivaltime": "arrival_time",
	"flightduration": "flight_duration",
	"departurehour": "departure_hour",
	"dayofweek": "day_of_week",
	"monthofyear": "month_of_year",
	"isweekend": "is_weekend",
	"holidayseason": "holiday_season",
	"scrapedate": "scrape_date",
	"scrapedatetime": "scrape_timestamp",
	"scrapetimestamp": "scrape_timestamp",
	"website": "website",
	"source": "website",
	"currentprice": "current_price",
	"price": "current_price",
	"fare": "current_price",
	"currency": "currency",
}
HEADER_ALIASES_BY_SOURCE = {
	"yatra": {
		**COMMON_HEADER_ALIASES,
		"fromairport": "departure_airport",
		"toairport": "arrival_airport",
	},
	"easemytrip": {
		**COMMON_HEADER_ALIASES,
		"fromsector": "departure_airport",
		"tosector": "arrival_airport",
	},
}

CREATE_TABLE_SQL = f"""
CREATE TABLE IF NOT EXISTS {TABLE_NAME} (
	id BIGSERIAL PRIMARY KEY,
	airline VARCHAR(150),
	flight_number VARCHAR(20) NOT NULL,
	fare_class VARCHAR(40),
	departure_airport VARCHAR(150),
	departure_airport_code VARCHAR(3),
	arrival_airport VARCHAR(150),
	arrival_airport_code VARCHAR(3),
	stop_counts INTEGER,
	departure_date DATE NOT NULL,
	departure_time TIME NOT NULL,
	arrival_time TIME,
	flight_duration VARCHAR(40),
	departure_hour SMALLINT,
	day_of_week VARCHAR(12),
	month_of_year SMALLINT,
	is_weekend BOOLEAN,
	holiday_season VARCHAR(80),
	scrape_date DATE,
	scrape_timestamp TIMESTAMPTZ NOT NULL,
	days_until_departure INTEGER,
	website VARCHAR(80) NOT NULL,
	current_price NUMERIC(12, 2),
	currency VARCHAR(3),
	CONSTRAINT {UNIQUE_CONSTRAINT_NAME} UNIQUE (
		website, flight_number, departure_date, departure_time, scrape_timestamp
	)
)
"""
INSERT_SQL = f"""
INSERT INTO {TABLE_NAME} ({", ".join(CSV_COLUMNS)})
VALUES %s
ON CONFLICT ON CONSTRAINT {UNIQUE_CONSTRAINT_NAME} DO NOTHING
RETURNING id
"""

logger = logging.getLogger(__name__)


def _normalize_header(value: str) -> str:
	"""Convert a CSV header into the canonical alias lookup form."""
	value = value.replace("\ufeff", "").strip().lower()
	value = re.sub(r"\.\d+$", "", value)
	return re.sub(r"[^a-z0-9]", "", value)


def _source_name(path: Path) -> str | None:
	"""Infer the source name from the CSV path for source-specific aliases."""
	path_text = str(path).lower()
	if "easemytrip" in path_text or "ease_my_trip" in path_text:
		return "easemytrip"
	if "yatra" in path_text:
		return "yatra"
	return None


def _header_indices(headers: Sequence[str], path: Path) -> dict[str, list[int]]:
	"""Map recognized source headers to destination columns and source positions."""
	source = _source_name(path)
	aliases = HEADER_ALIASES_BY_SOURCE.get(source or "", COMMON_HEADER_ALIASES)
	indices: dict[str, list[int]] = {}
	for index, header in enumerate(headers):
		if re.match(r"(?i)^unnamed(?::|$)", header.strip()):
			continue
		destination = aliases.get(_normalize_header(header))
		if destination:
			indices.setdefault(destination, []).append(index)
	return indices


def _parse_value(value: str, column: str) -> object | None:
	"""Convert one CSV cell into the corresponding PostgreSQL value type."""
	value = value.strip()
	if not value:
		return None
	if column in STRING_COLUMNS:
		return value
	if column in INTEGER_COLUMNS:
		try:
			return int(value)
		except ValueError:
			return None
	if column in BOOLEAN_COLUMNS:
		if value.lower() in {"true", "t", "yes", "y", "1"}:
			return True
		if value.lower() in {"false", "f", "no", "n", "0"}:
			return False
		return None
	if column == "current_price":
		try:
			return Decimal(re.sub(r"[^0-9.\-]", "", value))
		except InvalidOperation:
			return None
	if column in DATE_COLUMNS:
		try:
			return date.fromisoformat(value[:10])
		except ValueError:
			return None
	if column in TIME_COLUMNS:
		try:
			return time.fromisoformat(value)
		except ValueError:
			return None
	if column == "scrape_timestamp":
		try:
			timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
		except ValueError:
			return None
		return timestamp if timestamp.tzinfo else timestamp.replace(tzinfo=timezone.utc)
	return value


def iter_csv_rows(path: Path) -> Iterator[tuple[object | None, ...]]:
	"""Yield valid database records from a CSV one row at a time."""
	with path.open("r", newline="", encoding="utf-8-sig") as csv_file:
		reader = csv.reader(csv_file)
		headers = next(reader, None)
		if not headers:
			logger.warning("Skipping empty CSV: %s", path)
			return

		indices = _header_indices(headers, path)
		missing = [
			column
			for column in REQUIRED_COLUMNS
			if column not in indices and not (column == "website" and _source_name(path))
		]
		if missing:
			logger.error("Skipping %s; required CSV fields missing: %s", path, missing)
			return

		website_fallback = {
			"yatra": "Yatra",
			"easemytrip": "EaseMyTrip",
		}.get(_source_name(path) or "")
		for row_number, cells in enumerate(reader, start=2):
			record: dict[str, object | None] = {}
			for column, positions in indices.items():
				for position in positions:
					if position >= len(cells):
						continue
					parsed = _parse_value(cells[position], column)
					if parsed is not None:
						record[column] = parsed
						break
			if not record.get("website") and website_fallback:
				record["website"] = website_fallback
			if any(record.get(column) is None for column in REQUIRED_COLUMNS):
				logger.warning("Skipping row %d in %s with incomplete identity", row_number, path)
				continue
			yield tuple(record.get(column) for column in CSV_COLUMNS)


def get_database_url() -> str:
	"""Load the Neon connection URL without exposing its value in logs."""
	load_dotenv(PROJECT_ROOT / ".env", override=False)
	load_dotenv(PROJECT_ROOT / ".env.local", override=False)
	url = (
		os.getenv("NEON_DB_URL")
		or os.getenv("DATABASE_URL")
		or os.getenv("DATABASE_URL_UNPOOLED")
	)
	if not url:
		raise RuntimeError(
			"Set NEON_DB_URL in .env or provide DATABASE_URL in the environment."
		)
	if url.startswith("postgres://"):
		url = "postgresql://" + url.removeprefix("postgres://")
	return url


def _execute(connection_pool: SimpleConnectionPool, statement: str) -> None:
	"""Execute a schema statement using a pooled connection."""
	connection = connection_pool.getconn()
	try:
		with connection:
			with connection.cursor() as cursor:
				cursor.execute(statement)
	finally:
		connection_pool.putconn(connection)


def _insert_batch(
	connection_pool: SimpleConnectionPool,
	rows: Sequence[tuple[object | None, ...]],
) -> int:
	"""Insert one bounded batch and return the number of newly stored rows."""
	connection = connection_pool.getconn()
	try:
		with connection:
			with connection.cursor() as cursor:
				execute_values(cursor, INSERT_SQL, rows, page_size=len(rows))
				return len(cursor.fetchall())
	finally:
		connection_pool.putconn(connection)


def ingest_csv(
	path: Path,
	connection_pool: SimpleConnectionPool,
	batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
	"""Ingest one CSV in bounded batches and return newly inserted row count."""
	inserted_rows = 0
	batch: list[tuple[object | None, ...]] = []
	for row in iter_csv_rows(path):
		batch.append(row)
		if len(batch) >= batch_size:
			inserted_rows += _insert_batch(connection_pool, batch)
			batch.clear()
	if batch:
		inserted_rows += _insert_batch(connection_pool, batch)
	logger.info("Inserted %d new row(s) from %s", inserted_rows, path)
	return inserted_rows


def main(argv: Sequence[str] | None = None) -> int:
	"""Create the table and ingest the configured scraper CSV files."""
	parser = argparse.ArgumentParser(
		description="Ingest Yatra and EaseMyTrip flight CSVs into Neon Postgres."
	)
	parser.add_argument(
		"--csv",
		type=Path,
		action="append",
		default=None,
		help="CSV path to ingest; repeat to provide multiple files.",
	)
	parser.add_argument(
		"--batch-size",
		type=int,
		default=DEFAULT_BATCH_SIZE,
		help="Maximum rows per database insert transaction.",
	)
	args = parser.parse_args(argv)
	logging.basicConfig(
		level=logging.INFO,
		format="%(asctime)s %(levelname)s %(name)s: %(message)s",
	)
	if args.batch_size < 1:
		parser.error("--batch-size must be greater than zero")

	csv_paths = args.csv or list(DEFAULT_CSV_PATHS)
	existing_paths = [path for path in csv_paths if path.is_file()]
	for path in csv_paths:
		if path not in existing_paths:
			logger.warning("CSV not found; skipping: %s", path)
	if not existing_paths:
		logger.error("No input CSV files found.")
		return 1

	connection_pool = None
	try:
		connection_pool = SimpleConnectionPool(
			MIN_POOL_SIZE,
			MAX_POOL_SIZE,
			get_database_url(),
		)
		_execute(connection_pool, CREATE_TABLE_SQL)
		inserted_rows = sum(
			ingest_csv(path, connection_pool, args.batch_size)
			for path in existing_paths
		)
		logger.info("Ingestion complete: %d new row(s)", inserted_rows)
		return 0
	except (OSError, csv.Error, psycopg2.Error, RuntimeError):
		logger.exception("Database ingestion failed")
		return 1
	finally:
		if connection_pool is not None:
			connection_pool.closeall()


if __name__ == "__main__":
	sys.exit(main())