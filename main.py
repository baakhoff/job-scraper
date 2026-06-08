"""Typer CLI entrypoint for linkedin-job-parser.

Wires together the scraper -> parser -> filters -> storage pipeline behind a
small command-line interface.

    python main.py search --keywords "python backend" --location "Berlin"
    python main.py list --keyword remote
    python main.py new
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from config import config
from src.filters import (
    dedupe,
    filter_by_workplace_type,
    sort_by_posted_desc,
)
from src.models import JobListing, SearchParams, WorkplaceType
from src.parser import parse_detail_html, parse_search_html
from src.scraper import LinkedInScraper, RateLimiter
from src.storage import Storage


def _force_utf8_stdio() -> None:
    """Switch stdio to UTF-8 with a replacement fallback.

    Windows consoles often default to cp1252, which can't encode characters
    that show up in real job titles/locations (accents, em dashes), so output
    would otherwise crash the run.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


_force_utf8_stdio()

app = typer.Typer(help="Scrape and parse public LinkedIn job listings.")
console = Console()

# Marker file holding the timestamp of the last ``new`` check, so the command
# can report only listings added since then (drives a Telegram poller).
_LAST_CHECK_FILE = Path(config.db_path).with_name(".last_new_check")


async def _run_search(
    params: SearchParams, max_results: int, *, with_details: bool = False
) -> list[JobListing]:
    """Drive the async scraper/parser pipeline for one search.

    When ``with_details`` is set, each listing is enriched with an extra
    (rate-limited) fetch of its guest detail page — full description, seniority,
    employment type, job function, industries, applicant count.
    """
    rate_limiter = RateLimiter(
        delay_min=config.request_delay_min,
        delay_max=config.request_delay_max,
    )
    async with LinkedInScraper(
        rate_limiter=rate_limiter,
        user_agents=config.user_agents,
        max_pages=config.max_pages,
        max_results=max_results,
        max_retries=config.max_retries,
    ) as scraper:
        listings: list[JobListing] = []
        async for html in scraper.iter_pages(params):
            for raw in parse_search_html(html):
                try:
                    listings.append(JobListing.from_raw(raw))
                except Exception as exc:  # parse/validation failures are operational
                    console.print(f"[yellow]skipped a card:[/yellow] {exc}")
        listings = sort_by_posted_desc(dedupe(listings))[:max_results]
        if with_details:
            listings = await _enrich_with_details(scraper, listings)
        return listings


async def _persist(listings: list[JobListing]) -> tuple[int, str]:
    """Save listings to the configured database; return (new count, target label)."""
    async with Storage() as storage:
        new_count = await storage.save_jobs(listings)
        return new_count, _redact_url(storage.url)


async def _load_jobs(
    *, keyword: str | None = None, workplace_type: WorkplaceType | None = None, limit: int | None
) -> list[JobListing]:
    """Read stored listings from the configured database."""
    async with Storage() as storage:
        return await storage.get_jobs(
            keyword=keyword, workplace_type=workplace_type, limit=limit
        )


async def _load_new_jobs(since: datetime) -> list[JobListing]:
    """Read listings first seen since ``since`` from the configured database."""
    async with Storage() as storage:
        return await storage.get_new_jobs(since)


def _redact_url(url: str) -> str:
    """Mask any password in a SQLAlchemy URL before printing it."""
    if "://" not in url or "@" not in url:
        return url
    scheme, rest = url.split("://", 1)
    creds, host = rest.split("@", 1)
    if ":" in creds:
        user = creds.split(":", 1)[0]
        creds = f"{user}:***"
    return f"{scheme}://{creds}@{host}"


async def _enrich_with_details(
    scraper: LinkedInScraper, listings: list[JobListing]
) -> list[JobListing]:
    """Fetch each listing's detail page and merge in non-empty extra fields."""
    enriched: list[JobListing] = []
    for index, listing in enumerate(listings):
        if index > 0:
            await scraper.rate_limiter.wait()
        html = await scraper.fetch_detail(listing.job_id)
        if not html.strip():
            enriched.append(listing)
            continue
        detail = parse_detail_html(html)
        updates = {k: v for k, v in detail.items() if v is not None}
        if not updates:
            enriched.append(listing)
            continue
        try:
            # Re-validate through the model rather than model_copy so detail
            # strings (e.g. company_url) get the same coercion as search fields.
            merged = {**listing.model_dump(), **updates}
            enriched.append(JobListing(**merged))
        except Exception as exc:  # bad detail markup shouldn't drop the base listing
            console.print(f"[yellow]detail merge skipped for {listing.job_id}:[/yellow] {exc}")
            enriched.append(listing)
    return enriched


def _render_table(listings: list[JobListing], title: str) -> None:
    """Print listings as a rich table."""
    if not listings:
        console.print(f"[yellow]No listings to show for: {title}[/yellow]")
        return

    table = Table(title=title, show_lines=False, expand=True)
    table.add_column("Posted", style="dim", no_wrap=True)
    table.add_column("Title", style="bold cyan")
    table.add_column("Company", style="green")
    table.add_column("Location")
    table.add_column("Type", no_wrap=True)

    for job in listings:
        posted = job.posted_at.date().isoformat() if job.posted_at else "-"
        wtype = job.workplace_type.value.replace("_", "-") if job.workplace_type else "-"
        table.add_row(posted, job.title, job.company, job.location or "-", wtype)

    console.print(table)
    console.print(f"[dim]{len(listings)} listing(s).[/dim]")


@app.command()
def search(
    keywords: str = typer.Option(..., "--keywords", "-k", help="Search keywords."),
    location: str = typer.Option(None, "--location", "-l", help="Location filter."),
    geo_id: str = typer.Option(None, help="LinkedIn geoId (more reliable than location)."),
    workplace_type: WorkplaceType = typer.Option(
        None, "--workplace-type", "-w", help="remote / hybrid / on_site."
    ),
    max_results: int = typer.Option(
        config.max_results, "--max-results", "-n", help="Max listings to fetch."
    ),
    details: bool = typer.Option(
        False,
        "--details",
        "-d",
        help="Enrich each listing via its detail page (full description, "
        "seniority, employment type, applicant count). One extra request per job.",
    ),
) -> None:
    """Run a job search, persist the results, and print them as a table."""
    params = SearchParams(
        keywords=keywords, location=location, geo_id=geo_id, workplace_type=workplace_type
    )
    console.print(
        f"[bold]Searching[/bold] '{keywords}'"
        + (f" in '{location}'" if location else "")
        + f" (up to {max_results})..."
        + (" [dim](with details)[/dim]" if details else "")
    )
    listings = asyncio.run(_run_search(params, max_results, with_details=details))

    if workplace_type is not None:
        listings = filter_by_workplace_type(listings, [workplace_type])

    new_count, target = asyncio.run(_persist(listings))

    _render_table(listings, f"Results for '{keywords}'")
    console.print(
        f"[green]Stored {len(listings)} listing(s)[/green] "
        f"([bold]{new_count}[/bold] new) -> {target}"
    )


@app.command(name="list")
def list_jobs(
    keyword: str = typer.Option(None, "--keyword", "-k", help="Filter by title/company substring."),
    workplace_type: WorkplaceType = typer.Option(
        None, "--workplace-type", "-w", help="remote / hybrid / on_site."
    ),
    limit: int = typer.Option(50, "--limit", "-n", help="Max rows to show."),
) -> None:
    """List job listings already stored in the database."""
    listings = asyncio.run(
        _load_jobs(keyword=keyword, workplace_type=workplace_type, limit=limit)
    )
    _render_table(listings, "Saved listings")


@app.command()
def new(
    reset: bool = typer.Option(
        False, "--reset", help="Reset the marker to now without showing results."
    ),
) -> None:
    """Show jobs added since the last ``new`` check (Telegram integration hook).

    Reads the timestamp of the previous check, prints everything first seen
    after it, then advances the marker to now.
    """
    now = datetime.now(UTC)

    if reset:
        _write_last_check(now)
        console.print("[green]Marker reset to now.[/green]")
        return

    since = _read_last_check()
    listings = asyncio.run(_load_new_jobs(since))
    _render_table(listings, f"New since {since.isoformat(timespec='seconds')}")
    _write_last_check(now)


def _read_last_check() -> datetime:
    """Read the last-check timestamp; default to epoch on first run."""
    try:
        raw = _LAST_CHECK_FILE.read_text(encoding="utf-8").strip()
        return datetime.fromisoformat(raw)
    except (OSError, ValueError):
        return datetime.fromtimestamp(0, tz=UTC)


def _write_last_check(when: datetime) -> None:
    """Persist the last-check timestamp marker."""
    _LAST_CHECK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _LAST_CHECK_FILE.write_text(when.isoformat(), encoding="utf-8")


if __name__ == "__main__":
    app()
