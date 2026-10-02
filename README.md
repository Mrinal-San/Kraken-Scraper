# Skyline Sarathi: Flight Data Collection

This project collects flight fares with browser-based scrapers, saves them to local CSV files, and can load the Yatra and EaseMyTrip CSVs into Neon Postgres.

## Run from PowerShell

Run commands from the repository root, the folder containing `requirements.txt`.

### Prerequisites

- Python 3.11 or newer. Python 3.12 is recommended.
- Internet access and permission to use the flight-search websites.
- Firefox installed by Playwright (the setup command is below).
- A Neon project and database URL if you want to upload data to Postgres.
- Tk support in Python for the Pause/Play desktop window. It is included with the standard Windows Python installer.

### 1. Create and activate a virtual environment

Skip the create command if `.venv` already exists.

```powershell
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### 2. Install Python packages and Firefox

```powershell
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m playwright install firefox
```

### 3. Configure Neon for database ingestion

The ingestor looks for a connection URL in this order: `NEON_DB_URL`, `DATABASE_URL`, then `DATABASE_URL_UNPOOLED`. It loads values from the current environment, root `.env`, and root `.env.local`. The existing Neon setup for this repository uses `.env.local`.

If this is a new machine and the repository is not linked to the project yet, install and sign in to the Neon CLI, then link the production branch:

```powershell
npm install --global neon
neon login
neon link --project-id fragrant-river-28261909 --branch production -y
```

`neon link` pulls the branch's database variables into a local env file. If the variables are missing later, run:

```powershell
neon env pull
```

Keep `.env` and `.env.local` private. Do not commit connection strings or paste them into source files.

### 4. Run a scraper

Run one scraper at a time from the repository root:

```powershell
python -m yatra_scraper.main
```

```powershell
python -m easemytrip_scraper.main
```

Both scrapers open a visible Firefox browser and a Pause/Play window. They can take a long time because each search has a configured delay. Resolve any challenge manually in the browser; the scrapers do not bypass CAPTCHA checks.

### 5. Upload the CSV data to Neon

After the CSV files have rows, run:

```powershell
python -m data_pipeline.ingest_to_db
```

By default, the command reads `yatra_scraper/Yatra_data.csv` and `easemytrip_scraper/Easemytrip_data.csv`, creates `flight_fares` if needed, and inserts records in batches of 500. To ingest only one file or use another CSV, pass `--csv`; repeat the option for multiple files:

```powershell
python -m data_pipeline.ingest_to_db --csv yatra_scraper/Yatra_data.csv
python -m data_pipeline.ingest_to_db --csv yatra_scraper/Yatra_data.csv --csv easemytrip_scraper/Easemytrip_data.csv --batch-size 250
```

The local CSV files are not deleted after ingestion. Existing database rows with the same website, flight number, departure date/time, and scrape timestamp are skipped by the database's unique constraint.

### 6. View the rows in Neon

Open the [Neon Console](https://console.neon.tech/), select the Skyline Sarathi project, and open **Postgres database** > **SQL Editor**. Select the `production` branch and the database, then run:

```sql
SELECT *
FROM flight_fares
ORDER BY scrape_timestamp DESC
LIMIT 100;
```

To summarize the rows by source:

```sql
SELECT website, COUNT(*) AS row_count, MAX(scrape_timestamp) AS latest_scrape
FROM flight_fares
GROUP BY website
ORDER BY website;
```

See Neon’s [SQL Editor guide](https://neon.com/docs/get-started/query-with-neon-sql-editor.md) for more ways to query and export results.

## Project in Simple Terms

The data flow has three parts:

1. `main.py` opens the website, searches routes and dates, scrolls through results, and yields raw page or card text. It owns browser behavior, retries, pause controls, and checkpoints.
2. `feature_engineering.py` turns that raw text into structured flight rows and writes those rows to the scraper's CSV. Parsing and output are streamed so a full search result does not need to stay in memory.
3. `data_pipeline/ingest_to_db.py` reads the CSVs one row at a time, converts values to database types, and sends bounded batches to Neon.

The CSV is a local, human-readable copy of the scraped data. Neon is the central database copy for querying and analysis. The pipeline currently loads Yatra and EaseMyTrip by default; the other scraper folders are not included in its default CSV list.

## Scraper Behavior

Yatra and EaseMyTrip use the same default schedule:

- Six airports: `DEL`, `BOM`, `BLR`, `CCU`, `HYD`, and `MAA`.
- Every direction between different airports.
- Five departure dates at T+1, T+7, T+15, T+30, and T+45 days.
- Economy and Business fares.
- 30 routes x 5 dates x 2 fare classes = 300 searches per run.

Each scraper starts with tomorrow's date unless it resumes from a checkpoint. Results are filtered to the requested origin and destination before they are written. Rows within a search are deduplicated by their scrape identity.

### Output and recovery files

| Scraper | CSV output | Checkpoint | Pause file |
| --- | --- | --- | --- |
| Yatra | `yatra_scraper/Yatra_data.csv` | `yatra_scraper/checkpoint.json` | `yatra_scraper/pause_scraper.pause` |
| EaseMyTrip | `easemytrip_scraper/Easemytrip_data.csv` | `easemytrip_scraper/checkpoint.json` | `easemytrip_scraper/pause_scraper.pause` |

The desktop **Pause** button pauses before the next request; the current request finishes first. For unattended runs, create the scraper's pause file to pause before the next request, and remove it to resume. When a run finishes, its checkpoint is cleared. If interrupted, run the same scraper command again to resume.

To start a fresh full run, remove the relevant checkpoint. To start with a fresh CSV as well, archive or remove that CSV separately; CSV output is append-only.

## Data Pipeline

`data_pipeline/ingest_to_db.py` is the single database entry point. It uses Python's `csv` module for streaming input, `psycopg2` for PostgreSQL, and `python-dotenv` for local environment files. It maps source headers to the `flight_fares` schema, parses dates/times/prices/booleans, skips rows missing required identity fields, and logs skipped or inserted records.

The table has a unique constraint on `(website, flight_number, departure_date, departure_time, scrape_timestamp)`. The insert uses `ON CONFLICT DO NOTHING`, so rerunning ingestion does not duplicate rows with the same identity. Adjust the memory/round-trip tradeoff with `--batch-size`.

### Holiday features

`data_pipeline/holiday_features.py` provides separate date features for model preparation. It adds `is_holiday_season` and `days_to_nearest_holiday` to a Pandas DataFrame, using configured fixed holidays, 2025/2026 Holi and Diwali dates, peak travel months, and long-weekend bridge days. It can be tested with:

```powershell
python -m data_pipeline.holiday_features
```

## Project Layout

```text
kraken-scraper/
├── requirements.txt
├── README.md
├── yatra_scraper/
│   ├── main.py
│   ├── feature_engineering.py
│   ├── Yatra_data.csv
│   └── checkpoint.json
├── easemytrip_scraper/
│   ├── main.py
│   ├── feature_engineering.py
│   ├── Easemytrip_data.csv
│   └── checkpoint.json
├── makemytrip_scraper/
├── goibibo_scraper/
└── data_pipeline/
    ├── __init__.py
    ├── holiday_features.py
    └── ingest_to_db.py
```

`makemytrip_scraper/` and `goibibo_scraper/` contain additional scraper code. They are not part of the two-file default Neon ingestion list described above.

## Troubleshooting

### Firefox is missing

```powershell
python -m playwright install firefox
```

### Neon reports that no database URL is configured

Check that the root `.env.local` or `.env` contains `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, or `NEON_DB_URL`. Do not share the value; report only the variable name if asking for help.

### The ingestor says a CSV is missing

Run the corresponding scraper from the repository root first. Confirm the CSV path and name in the output table above.

### The ingestor inserts zero rows

Check that the CSV has data rows and complete `website`, `flight_number`, `departure_date`, `departure_time`, and `scrape_timestamp` values. Zero new inserts can also mean those rows already exist in Neon.

### A scraper resumes at an unexpected search

Inspect that scraper's `checkpoint.json`. It records the original start date, next iteration, route, departure date, and fare class. Remove that checkpoint only if you intend to begin a new run.
