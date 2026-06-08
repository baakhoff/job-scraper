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
| Input   | `config.py`       | Search defaults, delays, enrichment/people flags.         |
| Models  | `src/models.py`   | Pydantic: `SearchParams`, `JobListing`, `Position`, `Company`, `CompanyPerson`. |
| Scrape  | `src/scraper.py`  | Async httpx fetcher: jobs search/detail + `fetch_company` / `search_people`. |
| Parse   | `src/parser.py`   | BeautifulSoup: job / company / people HTML → raw dicts.    |
| Filter  | `src/filters.py`  | In-process narrowing: keywords, workplace type, dedupe.   |
| People  | `src/people.py`   | Pluggable CEO/Founder discovery (`PeopleProvider`).       |
| Export  | `src/export.py`   | Row dicts → CSV / JSON (stdlib).                           |
| Store   | `src/storage.py`  | async SQLAlchemy 2.0 → Postgres/SQLite; relational model + dedupe. |
| CLI     | `main.py`         | Typer entrypoint wiring the search pipeline.              |
| Web     | `web.py` + `templates/index.html` | FastAPI JSON API + tabbed SPA (Jobs/Companies/Explore). |

Data flow: `SearchParams → scraper (HTML pages) → parser (raw dicts) →
JobListing.from_raw → filters → Storage.save_search_results`.

Keep modules single-responsibility. The parser emits plain dicts (no
validation); `JobListing.from_raw` is the one place that cleans and validates.

### Relational model (`src/storage.py`)

Four tables: `positions`, `companies`, `job_listings` (kept by name, now with
nullable `position_id`/`company_id` FKs), and `company_people`. The
Position↔Company many-to-many is **derived** from listings via a `DISTINCT`
query — there is no link table. De-duplication is insert-if-absent: listings by
`job_id`, companies by slug (else normalized name), positions by
`(keyword, location)`, people by `profile_url` (else `(company_id, name)`).
`init_db` migrates old DBs in place (SQLite + Postgres column-adds) and
backfills companies for legacy listings.

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
`aiosqlite`), FastAPI + uvicorn (web UI), typer (CLI), structlog. Tooling: ruff
(line length 100), mypy `--strict`, pytest (`asyncio_mode = auto`).

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
- **CEO/Founder (people) search is best-effort and OFF by default.** LinkedIn's
  public *people* search is generally login-gated (the endpoint audit never
  validated it), so `LinkedInPeopleProvider` usually returns nothing. It sits
  behind `PeopleProvider` in `src/people.py` so a real data source can replace
  it without touching the rest; the default is `NullPeopleProvider`. Company
  enrichment (`fetch_company` / `parse_company_html`) is similarly unproven and
  fragile — both degrade to "nothing found", never crash.

## Conventions

- Async throughout the scrape path; the CLI bridges via `asyncio.run`.
- `job_id` is the stable natural key — use it for dedupe and DB upserts.
- Run `ruff check .` and `mypy .` before committing.
