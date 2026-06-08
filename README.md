# linkedin-job-parser

Scrape and parse **public** LinkedIn job listings into structured, filterable
data. No login or LinkedIn account is used — the tool hits LinkedIn's public
guest jobs endpoint, parses the returned HTML job cards, and stores normalized
records in a local SQLite database.

## Pipeline

```
SearchParams ─▶ scraper (httpx, async, paginated)
            ─▶ parser  (BeautifulSoup HTML → raw dicts)
            ─▶ models  (validate/normalize → JobListing)
            ─▶ filters (keywords, workplace type, dedupe)
            ─▶ storage (SQLite via SQLAlchemy)
```

## Usage

```bash
# install (editable, with dev extras) — or use `uv venv && uv pip install -e ".[dev]"`
pip install -e ".[dev]"

# run a search and persist results to SQLite
python main.py search --keywords "python developer" --location "Berlin" --max-results 25

# narrow by workplace type
python main.py search -k "data engineer" -l "Remote" --workplace-type remote

# list stored listings (optionally filtered)
python main.py list --keyword python --limit 20

# show jobs first seen since the previous `new` check (Telegram-notifier hook)
python main.py new
```

### Configuration

All settings live in [config.py](config.py) and can be overridden via `LJP_`-prefixed
environment variables or a `.env` file — e.g. `LJP_REQUEST_DELAY_MIN`,
`LJP_REQUEST_DELAY_MAX`, `LJP_MAX_RESULTS`, `LJP_DB_PATH`.

> ⚠️ **Be a good citizen.** Public data only, conservative request rates, honest
> User-Agent. The scraper jitters 2–5s between requests and backs off on `429`.
> Intended for personal, low-volume use; respect LinkedIn's Terms of Service.

## Tech stack

- **Python 3.11+**
- **httpx** — async HTTP client
- **BeautifulSoup4 + lxml** — HTML parsing
- **pydantic v2 / pydantic-settings** — models & configuration
- **SQLAlchemy 2.0** — SQLite persistence
- **typer + rich** — CLI and table rendering

## Development

```bash
uv run ruff check .   # lint
uv run mypy .         # strict type-check
uv run pytest -q      # offline test suite (parser/models/filters/storage/scraper)
```

See [CLAUDE.md](CLAUDE.md) for architecture notes and gotchas.
