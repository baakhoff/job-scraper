# CLAUDE.md

Guidance for working in this repository.

## What this is

`linkedin-job-parser` scrapes **public** LinkedIn job postings and turns them
into structured, filterable records stored in a database (PostgreSQL under
Docker, SQLite for local dev). It does **not** log in or use a LinkedIn account,
cookies, or the official API.

## Architecture

A linear async pipeline, one stage per module:

| Stage   | File              | Responsibility                                            |
| ------- | ----------------- | --------------------------------------------------------- |
| Input   | `config.py`       | Search defaults, delays, output path (pydantic-settings). |
| Models  | `src/models.py`   | `SearchParams` (query in) and `JobListing` (record out).  |
| Scrape  | `src/scraper.py`  | Async httpx fetcher, offset pagination, rate limiting.    |
| Parse   | `src/parser.py`   | BeautifulSoup: job-card HTML → loosely-typed raw dicts.   |
| Filter  | `src/filters.py`  | In-process narrowing: keywords, workplace type, dedupe.   |
| Store   | `src/storage.py`  | async SQLAlchemy 2.0 → Postgres/SQLite, upsert by `job_id`. |
| CLI     | `main.py`         | Typer entrypoint wiring the pipeline together.            |

Data flow: `SearchParams → scraper (HTML pages) → parser (raw dicts) →
JobListing.from_raw → filters → Storage.upsert_many`.

Keep modules single-responsibility. The parser emits plain dicts (no
validation); `JobListing.from_raw` is the one place that cleans and validates.

## Scraping approach

- **Endpoint:** the public guest API used by LinkedIn's logged-out jobs UI:
  `https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search`
- **No auth:** logged-out, so no cookies/credentials. Only public data.
- **Pagination:** offset-based via the `start` query param, page size **25**.
  Walk pages until an empty page or `max_pages` is reached.
- **Output:** the endpoint returns a fragment of `<li>` job cards (HTML, not
  JSON) — hence BeautifulSoup rather than a JSON decode.

## Tech stack

Python 3.11+, httpx (async), BeautifulSoup4 + lxml, pydantic v2 +
pydantic-settings, async SQLAlchemy 2.0 (PostgreSQL via `asyncpg`, SQLite via
`aiosqlite`), typer, structlog. Tooling: ruff (line length 100), mypy
`--strict`, pytest (`asyncio_mode = auto`).

## Gotchas

- **Rate limiting is mandatory.** LinkedIn soft-blocks / returns `429` quickly.
  Always go through `RateLimiter` (jittered delay), and back off exponentially
  on `429`. Defaults live in `config.py` (`request_delay_seconds`,
  `request_jitter_seconds`). Do not hammer the endpoint in tests.
- **HTML structure is fragile.** Selectors in `src/parser.py` (class names like
  `base-search-card__title`) break whenever LinkedIn ships markup changes.
  They are centralized at the top of the module on purpose — fix them there.
  Treat parse failures as expected operational events, not crashes.
- **Guest endpoint is undocumented** and can change shape or disappear. Keep
  the URL and query mapping (`SearchParams.to_query`) in one place.
- **Be a good citizen / legal.** Public data only, conservative request rates,
  honest `User-Agent`. Respect LinkedIn's Terms of Service; this is intended
  for personal, low-volume use.
- **Stubs everywhere.** Most functions raise `NotImplementedError`. Implement a
  stage end-to-end (scraper → parser → models) before wiring the next.

## Conventions

- Async throughout the scrape path; the CLI bridges via `asyncio.run`.
- `job_id` is the stable natural key — use it for dedupe and DB upserts.
- Run `ruff check .` and `mypy .` before committing.
