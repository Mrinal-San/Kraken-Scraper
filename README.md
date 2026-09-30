# Yatra Flight Scraper

A resilient Playwright-based scraper for collecting Yatra flight results across configured airport routes, travel dates, and fare classes.

The scraper is designed for long-running jobs. It provides a desktop Pause/Play control, incremental CSV persistence, retry handling, and checkpoint-based recovery after interruptions.

## Features

- Scrapes all origin and destination permutations for the configured airports.
- Excludes same-airport routes such as `DEL -> DEL`.
- Covers five departure dates at T+1, T+7, T+15, T+30, and T+45 days.
- Selects Economy and Business fares. Premium Economy is intentionally excluded to reduce the number of requests.
- Generates `30 routes x 5 dates x 2 fare classes = 300 requests`.
- Uses a visible Firefox browser so challenges can be handled manually when necessary.
- Rotates User-Agent values between request attempts.
- Applies a random 10-to-15-second delay before each request attempt.
- Retries failed requests up to three times with exponential backoff.
- Detects HTTP 403 and 429 responses as retryable failures.
- Scrolls through result pages and extracts loaded flight rows.
- Appends successful results to `Yatra_data.csv` immediately.
- Saves the next route, date, fare class, and iteration in a checkpoint file.
- Retries automatically after a Windows permission error prevents checkpoint replacement.
- Provides a Pause/Play desktop window that remains responsive while scraping.
- Supports a file-based pause mechanism for unattended operation.

## Requirements

- Windows, macOS, or Linux
- Python 3.10 or newer
- A working internet connection
- A Yatra account or access appropriate for the target website, if required by the site

Use the website in accordance with its terms, robots policy, and applicable laws. The scraper does not bypass CAPTCHA challenges. If a challenge appears, resolve it manually in the visible browser or allow the current permutation to be skipped after the configured timeout.

## Installation

Open PowerShell in the repository root.

### 1. Create a virtual environment

```powershell
python -m venv .venv
```

### 2. Activate the environment

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned
.\.venv\Scripts\Activate.ps1
```

### 3. Install Python dependencies

```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

### 4. Install the Playwright browser

```powershell
python -m playwright install firefox
```

## Run

Start the scraper from the repository root:

```powershell
python -m yatra_scraper.main
```

A small window named **Yatra scraper** opens while the scraper runs in the background. The browser also opens visibly so the workflow can be monitored.

The script automatically starts with:

- Tomorrow's date
- Iteration 1, unless a valid checkpoint exists
- The first configured airport route
- Economy, then Business for each date

## Iteration Order

The generated order is deterministic:

```text
Origin airport
  Destination airport
    T+1: Economy, Business
    T+7: Economy, Business
    T+15: Economy, Business
    T+30: Economy, Business
    T+45: Economy, Business
```

  The schedule starts tomorrow (T+1). Same-airport routes are skipped. For six airports, this produces 30 valid routes, or 300 requests total.

## Pause and Play

Use the **Pause** button in the desktop window to pause during the wait before a request. The current request is allowed to finish cleanly. Press **Play** to continue.

For a file-based pause, create this file from PowerShell:

```powershell
New-Item pause_scraper.pause
```

The scraper pauses before the next request while the file exists. Remove it to continue:

```powershell
Remove-Item pause_scraper.pause
```

This is useful when the scraper is running unattended or when a website challenge needs attention.

## Checkpoint Recovery

Progress is stored in:

```text
yatra_scraper_checkpoint.json
```

A checkpoint contains the next item to process, including the route bucket and fare class:

```json
{
  "start_date": "2026-09-29",
  "next_iteration": 91,
  "next_origin": "DEL",
  "next_destination": "BLR",
  "next_departure_date": "2026-11-13",
  "next_fare_class": "Economy"
}
```

If the process is interrupted by a network problem, system shutdown, or `Ctrl+C`, run the normal command again:

```powershell
python -m yatra_scraper.main
```

The scraper resumes from the checkpoint. The original start date is preserved even if the job continues after midnight.

When all permutations finish, the checkpoint is deleted automatically.

On Windows, another process or security tool can temporarily prevent the checkpoint file from being replaced. When this specific permission error occurs, the scraper waits 60 seconds and retries automatically. Keep the scraper running while it recovers; the desktop status and terminal output indicate the retry.

To deliberately start a completely new run, remove the checkpoint before launching:

```powershell
Remove-Item yatra_scraper_checkpoint.json -ErrorAction SilentlyContinue
```

The CSV is append-only. Remove or archive `Yatra_data.csv` before a fresh full run if duplicate output is not desired.

## Output

Successful results are appended to:

```text
Yatra_data.csv
```

The output contains flight details such as:

- Airline and flight number
- Origin and destination airport information
- Departure and arrival times
- Flight duration
- Stop count
- Departure date features
- Selected fare class
- Current price and currency
- Requested origin, destination, date, and fare class
- Scrape date and timestamp

Rows are written after each successful permutation, reducing data loss if the job stops later.

## Convert Flight Text to CSV

The `yatra_scraper.feature_engineering` module also provides a standalone converter for flight-results text. Place the text file in the repository root, then run:

```powershell
python -m yatra_scraper.feature_engineering flight_results.txt
```

By default, this creates `flight_results.csv` beside the input file. Choose a different output path with `--output`:

```powershell
python -m yatra_scraper.feature_engineering flight_results.txt --output parsed_flights.csv
```

If no input path is supplied, the converter looks for `flight_results.txt` in the current directory. The generated CSV includes parsed flight details and derived date and time features. This utility writes a new output file, so an existing file at the selected output path is replaced.

## Configuration

Runtime settings are defined near the top of `yatra_scraper/main.py`. Common settings include:

```python
DATE_OFFSETS = (0, 6, 14, 29, 44)
FARE_CLASSES = ("Economy", "Business")
REQUEST_DELAY_MIN_SECONDS = 10
REQUEST_DELAY_MAX_SECONDS = 15
MAX_RETRIES = 3
CHALLENGE_TIMEOUT_SECONDS = 30
CHECKPOINT_RETRY_DELAY_SECONDS = 60
```

Change configuration carefully. Lower request delays may reduce runtime but can increase the chance of rate limiting or CAPTCHA challenges.

## Troubleshooting

### Firefox is missing

Run:

```powershell
python -m playwright install firefox
```

### The scraper resumes from an unexpected iteration

Inspect:

```text
yatra_scraper_checkpoint.json
```

The `next_origin`, `next_destination`, `next_departure_date`, and `next_fare_class` fields show exactly what will run next.

### A CAPTCHA or challenge appears

The scraper waits for the configured challenge timeout. Resolve the challenge manually in the visible browser if possible. If it remains active, the current permutation is skipped and the checkpoint advances safely.

### The browser closes unexpectedly

Check the terminal output for the failed permutation. The request retry logic attempts failed requests up to three times, and completed rows remain in `Yatra_data.csv`.

### Duplicate rows appear after a restart

The scraper appends to the CSV by design. Checkpoint recovery prevents most repeated work, but remove the CSV before a deliberate clean run if necessary.

## Project Structure

```text
kraken-scraper/
├── requirements.txt
├── README.md
└── yatra_scraper/
    ├── feature_engineering.py
    └── main.py
```

## Validation

The current implementation has been validated for:

- Python syntax
- 300 generated combinations
- Five requested date offsets
- Economy and Business fare ordering
- November month navigation logic
- Checkpoint route/date/fare restoration
- Pause/Play controller imports and integration
