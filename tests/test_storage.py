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
