"""Web UI + JSON API for linkedin-job-parser.

A FastAPI app over the same scraper -> parser -> filters -> storage pipeline the
CLI uses. It serves one static page (a small tabbed SPA) plus a JSON API that
exposes the relational model:

    Position ──< Listings        Position >──< Companies        Company ──< People

Run it with::

    uv run python web.py            # then open http://127.0.0.1:8000
    # or: uv run uvicorn web:app --reload

Scraping/persistence logic is reused from ``main`` and ``src`` so the web UI and
CLI behave identically (same rate limiting, same dedupe, same DB).
"""

from __future__ import annotations

from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

from config import config

# Reuse the exact search pipeline the CLI runs.
from main import _run_search
from src.export import rows_to_csv, rows_to_json
from src.models import Company, CompanyPerson, JobListing, Position, SearchParams, WorkplaceType
from src.parser import parse_company_html
from src.people import get_people_provider
from src.scraper import LinkedInScraper, RateLimiter
from src.storage import Storage

_STATIC_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="LinkedIn Job Parser", description="Browse public LinkedIn job listings.")


# --------------------------------------------------------------------------- #
# Request bodies                                                              #
# --------------------------------------------------------------------------- #
class SearchRequest(BaseModel):
    """Body for a live search (Jobs and Companies tabs both use this)."""

    keywords: str = Field(..., min_length=1, description="What to search for.")
    location: str | None = Field(None, description="Optional location filter.")
    geo_id: str | None = Field(None, description="LinkedIn geoId (more reliable than location).")
    workplace_type: WorkplaceType | None = Field(None, description="remote / hybrid / on_site.")
    max_results: int = Field(25, ge=1, le=200, description="How many listings to fetch.")
    details: bool = Field(True, description="Fetch each job's detail page (slower).")
    posted_within_seconds: int | None = Field(None, description="Only jobs posted within N seconds.")


# --------------------------------------------------------------------------- #
# Serializers (domain models -> JSON-friendly dicts)                         #
# --------------------------------------------------------------------------- #
def _job_to_dict(job: JobListing) -> dict[str, object]:
    return {
        "job_id": job.job_id,
        "title": job.title,
        "company": job.company,
        "company_url": str(job.company_url) if job.company_url else None,
        "location": job.location,
        "workplace_type": job.workplace_type.value if job.workplace_type else None,
        "url": str(job.url) if job.url else None,
        "posted_at": job.posted_at.date().isoformat() if job.posted_at else None,
        "seniority": job.seniority,
        "employment_type": job.employment_type,
        "job_function": job.job_function,
        "industries": job.industries,
        "applicant_count": job.applicant_count,
        "salary": job.salary,
        "description_snippet": job.description_snippet,
        "description": job.description,
    }


def _company_to_dict(company: Company) -> dict[str, object]:
    return {
        "id": company.id,
        "name": company.name,
        "company_url": str(company.company_url) if company.company_url else None,
        "slug": company.slug,
        "location": company.location,
        "industry": company.industry,
        "company_size": company.company_size,
        "website": company.website,
        "description": company.description,
        "listing_count": company.listing_count,
    }


def _person_to_dict(person: CompanyPerson) -> dict[str, object]:
    return {
        "id": person.id,
        "name": person.name,
        "headline": person.headline,
        "profile_url": str(person.profile_url) if person.profile_url else None,
        "keyword": person.keyword,
        "source": person.source,
    }


def _position_to_dict(position: Position) -> dict[str, object]:
    return {
        "id": position.id,
        "keyword": position.keyword,
        "location": position.location,
        "company_count": position.company_count,
        "listing_count": position.listing_count,
    }


def _build_scraper() -> LinkedInScraper:
    """A scraper configured from settings (for enrichment / people lookups)."""
    return LinkedInScraper(
        rate_limiter=RateLimiter(config.request_delay_min, config.request_delay_max),
        user_agents=config.user_agents,
        max_pages=config.max_pages,
        max_retries=config.max_retries,
    )


# --------------------------------------------------------------------------- #
# Page                                                                        #
# --------------------------------------------------------------------------- #
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    """Serve the single-page UI."""
    return FileResponse(_STATIC_DIR / "index.html")


# --------------------------------------------------------------------------- #
# Search (Jobs + Companies)                                                   #
# --------------------------------------------------------------------------- #
@app.post("/api/search")
async def api_search(req: SearchRequest) -> dict[str, object]:
    """Run a live job search, persist it relationally (deduped), and return rows.

    Hits LinkedIn's public endpoint and is rate-limited, so it can take a while.
    """
    params = SearchParams(
        keywords=req.keywords,
        location=req.location or None,
        geo_id=req.geo_id or None,
        workplace_type=req.workplace_type,
        posted_within_seconds=req.posted_within_seconds,
    )
    listings = await _run_search(params, req.max_results, with_details=req.details)
    async with Storage() as storage:
        counts = await storage.save_search_results(
            listings, keyword=req.keywords, location=params.location
        )
    return {
        "count": len(listings),
        "new_count": counts["new_listings"],
        "new_companies": counts["new_companies"],
        "position_id": counts["position_id"],
        "jobs": [_job_to_dict(job) for job in listings],
    }


@app.post("/api/companies/search")
async def api_companies_search(req: SearchRequest) -> dict[str, object]:
    """Run a search and return the companies hiring for that position."""
    params = SearchParams(
        keywords=req.keywords,
        location=req.location or None,
        geo_id=req.geo_id or None,
        workplace_type=req.workplace_type,
        posted_within_seconds=req.posted_within_seconds,
    )
    listings = await _run_search(params, req.max_results, with_details=req.details)
    async with Storage() as storage:
        counts = await storage.save_search_results(
            listings, keyword=req.keywords, location=params.location
        )
        position_id = int(counts["position_id"])
        companies = await storage.get_companies_for_position(position_id)
    return {
        "count": len(companies),
        "position_id": position_id,
        "new_companies": counts["new_companies"],
        "companies": [_company_to_dict(c) for c in companies],
    }


# --------------------------------------------------------------------------- #
# Explore (read stored data)                                                  #
# --------------------------------------------------------------------------- #
@app.get("/api/positions")
async def api_positions() -> dict[str, object]:
    """All searched positions, with company/listing counts."""
    async with Storage() as storage:
        positions = await storage.get_positions()
    return {"count": len(positions), "positions": [_position_to_dict(p) for p in positions]}


@app.get("/api/positions/{position_id}/companies")
async def api_position_companies(position_id: int) -> dict[str, object]:
    """Companies hiring for a position."""
    async with Storage() as storage:
        companies = await storage.get_companies_for_position(position_id)
    return {"count": len(companies), "companies": [_company_to_dict(c) for c in companies]}


@app.get("/api/positions/{position_id}/listings")
async def api_position_listings(position_id: int) -> dict[str, object]:
    """Job listings saved under a position."""
    async with Storage() as storage:
        listings = await storage.get_listings_for_position(position_id)
    return {"count": len(listings), "listings": [_job_to_dict(j) for j in listings]}


@app.get("/api/companies")
async def api_companies(keyword: str | None = None, limit: int = 200) -> dict[str, object]:
    """All stored companies (optional name filter)."""
    async with Storage() as storage:
        companies = await storage.get_companies(keyword=keyword, limit=limit)
    return {"count": len(companies), "companies": [_company_to_dict(c) for c in companies]}


@app.get("/api/companies/{company_id}")
async def api_company(company_id: int) -> dict[str, object]:
    """Full company view: the company, its listings, and its people."""
    async with Storage() as storage:
        company = await storage.get_company(company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        listings = await storage.get_listings_for_company(company_id)
        people = await storage.get_people_for_company(company_id)
    return {
        "company": _company_to_dict(company),
        "listings": [_job_to_dict(job) for job in listings],
        "people": [_person_to_dict(p) for p in people],
    }


@app.get("/api/listings/{job_id}")
async def api_listing(job_id: str) -> dict[str, object]:
    """A single stored listing with all its fields."""
    async with Storage() as storage:
        listing = await storage.get_listing(job_id)
        if listing is None:
            raise HTTPException(status_code=404, detail="Listing not found")
    return {"listing": _job_to_dict(listing)}


# --------------------------------------------------------------------------- #
# Enrichment + people discovery                                              #
# --------------------------------------------------------------------------- #
@app.post("/api/companies/{company_id}/enrich")
async def api_enrich_company(company_id: int) -> dict[str, object]:
    """Best-effort: fetch the public company page and fill in extra fields."""
    async with Storage() as storage:
        company = await storage.get_company(company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")
        if not company.slug:
            return {"company": _company_to_dict(company), "note": "No company handle to enrich."}
        async with _build_scraper() as scraper:
            html = await scraper.fetch_company(company.slug)
        if not html.strip():
            return {
                "company": _company_to_dict(company),
                "note": "Company page unavailable (guest request was blocked).",
            }
        data = parse_company_html(html)
        updated = await storage.update_company(
            company_id,
            industry=data.get("industry"),
            company_size=data.get("company_size"),
            website=data.get("website"),
            description=data.get("description"),
        )
        return {"company": _company_to_dict(updated or company), "note": None}


@app.post("/api/companies/{company_id}/people")
async def api_company_people(
    company_id: int, keywords: str | None = None
) -> dict[str, object]:
    """Find leaders (CEO/Founder) at a company by keyword; persist and return them.

    People search is OFF by default (public people search is login-gated). When
    disabled or when nothing is found, a ``note`` explains why and the stored
    (possibly empty) people list is returned.
    """
    keyword_list = [
        k.strip()
        for k in (keywords.split(",") if keywords else config.people_search_keywords)
        if k.strip()
    ]
    async with Storage() as storage:
        company = await storage.get_company(company_id)
        if company is None:
            raise HTTPException(status_code=404, detail="Company not found")

        note: str | None = None
        if config.people_search_enabled and config.people_provider == "linkedin":
            async with _build_scraper() as scraper:
                provider = get_people_provider(scraper)
                found = await provider.search_people(company.name, keyword_list)
            if found:
                await storage.upsert_company_people(company_id, found)
            else:
                note = "No leaders found — public people search is usually login-gated."
        else:
            note = (
                "People search is disabled. Enable it with LJP_PEOPLE_SEARCH_ENABLED=true "
                "and LJP_PEOPLE_PROVIDER=linkedin (best-effort; usually login-gated)."
            )
        people = await storage.get_people_for_company(company_id)
    return {"count": len(people), "people": [_person_to_dict(p) for p in people], "note": note}


# --------------------------------------------------------------------------- #
# Export                                                                       #
# --------------------------------------------------------------------------- #
_EXPORT_FIELDS: dict[str, list[str]] = {
    "listings": [
        "job_id", "title", "company", "location", "workplace_type", "url",
        "posted_at", "seniority", "employment_type", "applicant_count",
        "company_url", "description_snippet",
    ],
    "companies": [
        "id", "name", "slug", "company_url", "location", "industry",
        "company_size", "website", "listing_count", "description",
    ],
    "people": ["id", "company_id", "name", "headline", "profile_url", "keyword", "source"],
}


async def _export_rows(entity: str, keyword: str | None) -> list[dict[str, object]]:
    """Build the row dicts for an export entity (listings | companies | people)."""
    async with Storage() as storage:
        if entity == "listings":
            jobs = await storage.get_jobs(keyword=keyword)
            return [_job_to_dict(job) for job in jobs]
        if entity == "companies":
            companies = await storage.get_companies(keyword=keyword)
            return [_company_to_dict(c) for c in companies]
        if entity == "people":
            return [
                {"company_id": company_id, **_person_to_dict(person)}
                for company_id, person in await storage.all_people()
            ]
    raise HTTPException(status_code=404, detail=f"Unknown export entity: {entity}")


@app.get("/api/export/{entity}.{fmt}")
async def api_export(entity: str, fmt: str, keyword: str | None = None) -> Response:
    """Download listings/companies/people as CSV or JSON (attachment)."""
    if entity not in _EXPORT_FIELDS:
        raise HTTPException(status_code=404, detail=f"Unknown export entity: {entity}")
    rows = await _export_rows(entity, keyword)
    fields = _EXPORT_FIELDS[entity]
    if fmt == "csv":
        body, media = rows_to_csv(rows, fields=fields), "text/csv"
    elif fmt == "json":
        body, media = rows_to_json(rows, fields=fields), "application/json"
    else:
        raise HTTPException(status_code=400, detail="Format must be csv or json")
    return Response(
        content=body,
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{entity}.{fmt}"'},
    )


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
