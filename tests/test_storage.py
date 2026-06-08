"""Tests for the async storage layer (upsert + new-since queries).

These run against SQLite (via ``aiosqlite``) for a zero-setup offline suite; the
same code path drives Postgres (via ``asyncpg``) under Docker. ``asyncio_mode =
auto`` (see pyproject.toml) lets the async tests run without explicit markers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio

from src.models import JobListing, WorkplaceType
from src.storage import Storage, resolve_database_url


def _job(job_id: str, **kw: object) -> JobListing:
    base: dict[str, object] = {"job_id": job_id, "title": "Engineer", "company": "Acme"}
    base.update(kw)
    return JobListing(**base)


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    """An initialized SQLite-backed Storage on a temp file, disposed on teardown."""
    store = Storage(str(tmp_path / "jobs.db"))
    await store.init_db()
    try:
        yield store
    finally:
        await store.dispose()


def test_resolve_database_url_normalizes_to_async_drivers() -> None:
    # A bare path is treated as a local SQLite file.
    assert resolve_database_url("output/jobs.db") == "sqlite+aiosqlite:///output/jobs.db"
    # Sync drivers are upgraded to their async equivalents.
    assert resolve_database_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert (
        resolve_database_url("postgresql://ljp:ljp@db:5432/ljp")
        == "postgresql+asyncpg://ljp:ljp@db:5432/ljp"
    )
    # An already-async URL is left untouched.
    assert (
        resolve_database_url("postgresql+asyncpg://ljp@db/ljp")
        == "postgresql+asyncpg://ljp@db/ljp"
    )


async def test_save_jobs_upserts_by_job_id(storage: Storage) -> None:
    assert await storage.save_jobs([_job("1", title="Old Title")]) == 1
    # Same id again => update in place, not a new row.
    assert await storage.save_jobs([_job("1", title="New Title"), _job("2")]) == 1

    jobs = {j.job_id: j for j in await storage.get_jobs()}
    assert set(jobs) == {"1", "2"}
    assert jobs["1"].title == "New Title"


async def test_get_jobs_filters_by_keyword_and_workplace_type(storage: Storage) -> None:
    await storage.save_jobs(
        [
            _job("1", title="Python Developer", workplace_type=WorkplaceType.REMOTE),
            _job("2", title="Java Developer", workplace_type=WorkplaceType.ON_SITE),
        ]
    )

    assert [j.job_id for j in await storage.get_jobs(keyword="python")] == ["1"]
    remote = await storage.get_jobs(workplace_type=WorkplaceType.REMOTE)
    assert [j.job_id for j in remote] == ["1"]


async def test_get_new_jobs_uses_first_seen_at(storage: Storage) -> None:
    await storage.save_jobs([_job("1")])
    cutoff = datetime.now(UTC)
    await storage.save_jobs([_job("2")])

    new = await storage.get_new_jobs(cutoff)
    assert [j.job_id for j in new] == ["2"]


async def test_round_trips_enrichment_fields(storage: Storage) -> None:
    await storage.save_jobs(
        [
            _job(
                "1",
                company_url="https://www.linkedin.com/company/acme",
                description="Full description.",
                employment_type="Full-time",
                job_function="Engineering",
                industries="Software Development",
                applicant_count=200,
            )
        ]
    )
    (job,) = await storage.get_jobs()
    assert str(job.company_url) == "https://www.linkedin.com/company/acme"
    assert job.description == "Full description."
    assert job.employment_type == "Full-time"
    assert job.applicant_count == 200


async def test_update_preserves_detail_fields_on_search_only_pass(storage: Storage) -> None:
    """A later search-only upsert must not wipe earlier detail enrichment."""
    await storage.save_jobs([_job("1", description="Rich detail.", applicant_count=42)])
    # Re-seen via a plain search (no detail fields) — enrichment should survive.
    await storage.save_jobs([_job("1", title="Updated Title")])

    (job,) = await storage.get_jobs()
    assert job.title == "Updated Title"
    assert job.description == "Rich detail."
    assert job.applicant_count == 42


# --------------------------------------------------------------------------- #
# Relational model: positions, companies, people                              #
# --------------------------------------------------------------------------- #
_ACME = "https://www.linkedin.com/company/acme"
_GLOBEX = "https://www.linkedin.com/company/globex"


async def test_save_search_results_links_positions_and_companies(storage: Storage) -> None:
    jobs = [
        _job("1", company="Acme", company_url=_ACME),
        _job("2", company="Acme", company_url=_ACME),
        _job("3", company="Globex", company_url=_GLOBEX),
    ]
    result = await storage.save_search_results(jobs, keyword="Python", location="Berlin")
    assert result["new_listings"] == 3
    assert result["new_companies"] == 2

    (position,) = await storage.get_positions()
    assert position.keyword == "Python"
    assert position.company_count == 2
    assert position.listing_count == 3
    assert position.id is not None

    companies = await storage.get_companies_for_position(position.id)
    # Sorted by listing count desc: Acme (2) before Globex (1).
    assert [(c.name, c.listing_count) for c in companies] == [("Acme", 2), ("Globex", 1)]


async def test_save_search_results_dedupes_on_rerun(storage: Storage) -> None:
    jobs = [_job("1", company="Acme", company_url=_ACME)]
    first = await storage.save_search_results(jobs, keyword="Python")
    second = await storage.save_search_results(jobs, keyword="Python")
    assert first["new_listings"] == 1 and first["new_companies"] == 1
    # Re-running the same search inserts nothing new.
    assert second["new_listings"] == 0 and second["new_companies"] == 0
    assert len(await storage.get_positions()) == 1
    assert len(await storage.get_companies()) == 1


async def test_company_dedupe_prefers_slug(storage: Storage) -> None:
    # Same slug, differently-cased name => one company.
    await storage.save_search_results(
        [_job("1", company="Acme Inc", company_url=_ACME)], keyword="a"
    )
    await storage.save_search_results(
        [_job("2", company="ACME", company_url=_ACME + "?trk=x")], keyword="b"
    )
    companies = await storage.get_companies()
    assert len(companies) == 1
    assert companies[0].slug == "acme"


async def test_company_view_returns_listings_and_people(storage: Storage) -> None:
    from src.models import CompanyPerson

    await storage.save_search_results(
        [_job("1", company="Acme", company_url=_ACME)], keyword="Python"
    )
    (company,) = await storage.get_companies()
    assert company.id is not None
    new = await storage.upsert_company_people(
        company.id,
        [CompanyPerson(name="Jane Doe", headline="CEO", keyword="CEO")],
    )
    assert new == 1
    # Dedupe: same person again => no new row.
    assert await storage.upsert_company_people(company.id, [CompanyPerson(name="Jane Doe")]) == 0

    people = await storage.get_people_for_company(company.id)
    assert [p.name for p in people] == ["Jane Doe"]
    listings = await storage.get_listings_for_company(company.id)
    assert [j.job_id for j in listings] == ["1"]


async def test_backfill_links_legacy_listings_to_companies(storage: Storage) -> None:
    # Simulate legacy data: rows saved via the old flat path (no company_id).
    await storage.save_jobs([_job("1", company="Acme", company_url=_ACME)])
    # A fresh init runs the backfill.
    await storage.init_db()
    companies = await storage.get_companies()
    assert [c.name for c in companies] == ["Acme"]
    assert companies[0].listing_count == 1


async def test_update_company_patches_enrichment(storage: Storage) -> None:
    await storage.save_search_results(
        [_job("1", company="Acme", company_url=_ACME)], keyword="Python"
    )
    (company,) = await storage.get_companies()
    assert company.id is not None
    updated = await storage.update_company(
        company.id, industry="Software", website="https://acme.example"
    )
    assert updated is not None
    assert updated.industry == "Software"
    assert updated.website == "https://acme.example"
