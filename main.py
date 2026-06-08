"""Typer CLI entrypoint for linkedin-job-parser.

Wires together the scraper -> parser -> filters -> storage pipeline behind a
small command-line interface.

    python main.py search "python backend" --location "Berlin"
    python main.py list
"""

from __future__ import annotations

import asyncio

import typer

from config import config
from src.filters import dedupe
from src.models import JobListing, SearchParams
from src.parser import parse_search_html
from src.scraper import LinkedInScraper, RateLimiter
from src.storage import Storage

app = typer.Typer(help="Scrape and parse public LinkedIn job listings.")


async def _run_search(params: SearchParams) -> list[JobListing]:
    """Drive the async scraper/parser pipeline for one search.

    Stub: iterate scraper pages, parse each to raw dicts, build ``JobListing``
    models, dedupe, and return them.
    """
    rate_limiter = RateLimiter(
        delay_seconds=config.request_delay_seconds,
        jitter_seconds=config.request_jitter_seconds,
    )
    async with LinkedInScraper(
        rate_limiter=rate_limiter,
        user_agent=config.user_agent,
        max_pages=config.max_pages,
    ) as scraper:
        listings: list[JobListing] = []
        async for html in scraper.iter_pages(params):
            raw = parse_search_html(html)
            listings.extend(JobListing.from_raw(item) for item in raw)
        return dedupe(listings)


@app.command()
def search(
    keywords: str = typer.Argument(..., help="Search keywords, e.g. 'python backend'."),
    location: str = typer.Option(None, help="Location filter."),
    geo_id: str = typer.Option(None, help="LinkedIn geoId (more reliable than location)."),
) -> None:
    """Run a job search and persist the results to the database."""
    params = SearchParams(keywords=keywords, location=location, geo_id=geo_id)
    listings = asyncio.run(_run_search(params))

    storage = Storage(config.db_path)
    storage.init_db()
    written = storage.upsert_many(listings)
    typer.echo(f"Stored {written} listing(s) to {config.db_path}.")


@app.command(name="list")
def list_jobs() -> None:
    """List job listings already stored in the database."""
    storage = Storage(config.db_path)
    for listing in storage.all_listings():
        typer.echo(f"{listing.job_id}\t{listing.title} @ {listing.company}")


if __name__ == "__main__":
    app()
