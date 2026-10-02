from __future__ import annotations

from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd


# Month/day keys keep recurring fixed holidays independent of a specific year.
FIXED_HOLIDAYS: dict[tuple[int, int], str] = {
	(1, 26): "Republic Day",
	(8, 15): "Independence Day",
	(10, 2): "Gandhi Jayanti",
	(12, 25): "Christmas Day",
}

# Festival dates vary by year. Holi is the main celebration day (Dhulandi).
VARIABLE_FESTIVALS: dict[int, dict[str, date]] = {
	2025: {
		"Holi": date(2025, 3, 14),
		"Diwali": date(2025, 10, 20),
	},
	2026: {
		"Holi": date(2026, 3, 4),
		"Diwali": date(2026, 11, 8),
	},
}

PEAK_TRAVEL_MONTHS = frozenset({1, 5, 6, 7, 12})

# Weekday numbers follow datetime.date.weekday(): Monday=0, Sunday=6.
LONG_WEEKEND_BRIDGE_OFFSETS = {
	1: -1,  # Tuesday holiday: the preceding Monday
	3: 1,   # Thursday holiday: the following Friday
	4: -1,  # Friday holiday: the preceding Thursday
}

DateLike = date | datetime | pd.Timestamp | str


def _as_date(target_date: DateLike) -> date:
	if pd.isna(target_date):
		raise ValueError("target_date must be a valid date")
	if isinstance(target_date, datetime):
		return target_date.date()
	if isinstance(target_date, date):
		return target_date
	return pd.Timestamp(target_date).date()


def _holiday_dates(first_year: int, last_year: int) -> list[date]:
	dates = {
		date(year, month, day)
		for year in range(first_year, last_year + 1)
		for month, day in FIXED_HOLIDAYS
	}
	for year in range(first_year, last_year + 1):
		dates.update(VARIABLE_FESTIVALS.get(year, {}).values())
	return sorted(dates)


def _long_weekend_bridge_dates(holidays: list[date]) -> set[date]:
	bridge_dates = set()
	for holiday in holidays:
		if offset := LONG_WEEKEND_BRIDGE_OFFSETS.get(holiday.weekday()):
			bridge_dates.add(holiday + timedelta(days=offset))
	return bridge_dates


def is_holiday_or_peak(target_date: DateLike) -> bool:
	"""Return whether a date is a configured holiday, peak season, or bridge day."""
	day = _as_date(target_date)
	if day.month in PEAK_TRAVEL_MONTHS:
		return True

	holidays = _holiday_dates(day.year - 1, day.year + 1)
	return day in holidays or day in _long_weekend_bridge_dates(holidays)


def get_days_to_nearest(target_date: DateLike) -> int:
	"""Return days until the nearest holiday on or after target_date."""
	day = _as_date(target_date)
	upcoming = [
		holiday
		for holiday in _holiday_dates(day.year, day.year + 1)
		if holiday >= day
	]
	if not upcoming:
		raise RuntimeError(f"No configured upcoming holiday found after {day}")
	return (upcoming[0] - day).days


def add_holiday_features(df: pd.DataFrame, date_col: str) -> pd.DataFrame:
	"""Return a copy with vectorized holiday-season and nearest-holiday features.

	Unparseable dates receive ``is_holiday_season=0`` and a missing nearest-holiday
	distance. Variable festivals are configured for 2025 and 2026; fixed holidays
	continue to recur in other years.
	"""
	if date_col not in df.columns:
		raise KeyError(f"Date column {date_col!r} was not found")

	result = df.copy()
	parsed_dates = pd.to_datetime(result[date_col], errors="coerce", format="mixed")
	valid_mask = parsed_dates.notna().to_numpy()
	valid_dates = parsed_dates[valid_mask]
	valid_positions = np.flatnonzero(valid_mask)
	feature_flags = parsed_dates.dt.month.isin(PEAK_TRAVEL_MONTHS).to_numpy().copy()

	if not valid_dates.empty:
		first_year = int(valid_dates.dt.year.min())
		last_year = int(valid_dates.dt.year.max())
		holiday_dates = _holiday_dates(first_year - 1, last_year + 1)
		flagged_dates = holiday_dates + sorted(
			_long_weekend_bridge_dates(holiday_dates)
		)
		flagged_values = np.array(
			[np.datetime64(day, "D").astype(np.int64) for day in flagged_dates],
			dtype=np.int64,
		)
		target_values = valid_dates.to_numpy(dtype="datetime64[D]").astype(np.int64)
		feature_flags[valid_positions] |= np.isin(target_values, flagged_values)

	result["is_holiday_season"] = feature_flags.astype(np.int8)

	days_to_nearest = pd.Series(pd.NA, index=result.index, dtype="Int64")
	if not valid_dates.empty:
		holiday_values = np.array(
			[
				np.datetime64(day, "D").astype(np.int64)
				for day in _holiday_dates(first_year, last_year + 1)
			],
			dtype=np.int64,
		)
		target_values = valid_dates.to_numpy(dtype="datetime64[D]").astype(np.int64)
		nearest_positions = np.searchsorted(holiday_values, target_values, side="left")
		days_to_nearest.iloc[valid_positions] = (
			holiday_values[nearest_positions] - target_values
		)
	result["days_to_nearest_holiday"] = days_to_nearest
	return result


if __name__ == "__main__":
	dummy_data = pd.DataFrame(
		{
			"departure_date": [
				"2025-03-13",  # Bridge day before Holi
				"2025-03-14",  # Holi
				"2025-08-15",  # Independence Day
				"2025-10-20",  # Diwali
				"2025-11-01",  # Outside configured dates and seasons
			],
			"route": ["DEL-BOM", "BOM-BLR", "DEL-MAA", "HYD-CCU", "BLR-DEL"],
		}
	)
	print(add_holiday_features(dummy_data, "departure_date").to_string(index=False))