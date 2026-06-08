# linkedin-job-parser

Scrape and parse **public** LinkedIn job listings into structured, filterable
data. No login or LinkedIn account is used — the tool hits LinkedIn's public
guest jobs endpoint, parses the returned HTML job cards, and stores normalized
records in a database (PostgreSQL under Docker; SQLite for zero-setup local use).

## Pipeline

```
SearchParams ─▶ scraper (httpx, async, paginated)
            ─▶ parser  (BeautifulSoup HTML → raw dicts)
            ─▶ models  (validate/normalize → JobListing)
            ─▶ filters (keywords, workplace type, dedupe)
            ─▶ storage (PostgreSQL / SQLite via async SQLAlchemy)
```

## Usage

```bash
# install (editable, with dev extras) — or use `uv venv && uv pip install -e ".[dev]"`
pip install -e ".[dev]"

# run a search and persist results (SQLite by default; set DATABASE_URL for Postgres)
python main.py search --keywords "python developer" --location "Berlin" --max-results 25

# narrow by workplace type
python main.py search -k "data engineer" -l "Remote" --workplace-type remote

# enrich each listing via its detail page (full description, seniority,
# employment type, job function, industries, applicant count). Costs one
# extra (rate-limited) request per job.
python main.py search -k "python" -l "Berlin" -n 25 --details

# list stored listings (optionally filtered)
python main.py list --keyword python --limit 20

# show jobs first seen since the previous `new` check (Telegram-notifier hook)
python main.py new
```

### What gets captured

Every search card yields `title`, `company`, **`company_url`** (the public
`/company/...` profile link), `location`, the job `url`, and `posted_at`. The
optional `--details` pass adds the full `description`, `seniority`,
`employment_type`, `job_function`, `industries`, and `applicant_count`.

See [docs/ENDPOINT_AUDIT.md](docs/ENDPOINT_AUDIT.md) for what the public guest
endpoints do and don't expose (pagination depth, date filters, geo ids), with
real request/response evidence.

### Configuration

All settings live in [config.py](config.py) and can be overridden via `LJP_`-prefixed
environment variables or a `.env` file — e.g. `LJP_REQUEST_DELAY_MIN`,
`LJP_REQUEST_DELAY_MAX`, `LJP_MAX_RESULTS`, `LJP_DB_PATH`.

The persistence backend is chosen by the `DATABASE_URL` environment variable
(an async SQLAlchemy URL). Leave it unset to use a local SQLite file
(`sqlite+aiosqlite:///./jobs.db`); set it to a `postgresql+asyncpg://…` URL to
use PostgreSQL. Docker Compose sets this automatically to the bundled `postgres`
service.

> ⚠️ **Be a good citizen.** Public data only, conservative request rates, honest
> User-Agent. The scraper jitters 2–5s between requests and backs off on `429`.
> Intended for personal, low-volume use; respect LinkedIn's Terms of Service.

## Docker

A multi-stage `Dockerfile` (python:3.12-slim, deps installed with `uv`,
non-root user) and a `docker-compose.yml` that bundles a **PostgreSQL 16**
service ship with the project. The `parser`/`scheduler` workers wait for
Postgres to be healthy (`depends_on: service_healthy`) and connect to it over
the compose network. Copy the env template first:

```bash
cp .env.example .env   # edit LJP_* / POSTGRES_* values to taste
```

```bash
# Build the image
docker compose build

# Run the search configured by LJP_SEARCH_* in .env (one-shot).
# Postgres is started and waited on automatically.
docker compose run --rm parser

# Run any CLI verb by overriding the command
docker compose run --rm parser python main.py list --limit 20
docker compose run --rm parser python main.py search -k "rust" -l "Remote" --details
```

Postgres data persists on the named volume `jobs-pgdata`, so it survives
container restarts. The DB connection is configured via `DATABASE_URL` (set by
Compose to the bundled `postgres` service); `POSTGRES_USER` / `POSTGRES_PASSWORD`
/ `POSTGRES_DB` / `POSTGRES_PORT` tune the service, and all `LJP_*` variables in
`.env` are passed through to the workers.

### Scheduled runs

The `scheduler` profile runs the configured search on a loop
(`LJP_SCHEDULE_INTERVAL` seconds, default hourly) and exposes a healthcheck that
goes unhealthy if the loop stops stamping its heartbeat:

```bash
docker compose --profile scheduler up -d
docker compose ps              # STATUS shows (healthy) once the first run lands
docker compose logs -f scheduler
```

`docker compose config` validates the full setup if you just want to lint it.

## Tech stack

- **Python 3.11+**
- **httpx** — async HTTP client
- **BeautifulSoup4 + lxml** — HTML parsing
- **pydantic v2 / pydantic-settings** — models & configuration
- **async SQLAlchemy 2.0** — PostgreSQL (`asyncpg`) / SQLite (`aiosqlite`) persistence
- **typer + rich** — CLI and table rendering

## Development

```bash
uv run ruff check .   # lint
uv run mypy .         # strict type-check
uv run pytest -q      # offline test suite (parser/models/filters/storage/scraper)
```

See [CLAUDE.md](CLAUDE.md) for architecture notes and gotchas.
