# linkedin-job-parser

Scrape and parse **public** LinkedIn job listings into structured, filterable
data. No login or LinkedIn account is used — the tool hits LinkedIn's public
guest jobs endpoint, parses the returned HTML job cards, and stores normalized
records in a local SQLite database.

> ⚠️ This is an early scaffold: most module internals are stubs (`NotImplementedError`).

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
# install (editable, with dev extras)
pip install -e ".[dev]"

# run a search
python main.py search "python backend" --location "Berlin"

# list stored listings
python main.py list
```

## Tech stack

- **Python 3.11+**
- **httpx** — async HTTP client
- **BeautifulSoup4 + lxml** — HTML parsing
- **pydantic v2 / pydantic-settings** — models & configuration
- **SQLAlchemy 2.0** — SQLite persistence
- **typer** — CLI

See [CLAUDE.md](CLAUDE.md) for architecture notes and gotchas.
