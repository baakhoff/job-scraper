"""Tests for the SQLite storage layer (upsert + new-since queries)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from src.models import JobListing, WorkplaceType
from src.storage import Storage


def _job(job_id: str, **kw: object) -> JobListing:
    base: dict[str, object] = {"job_id": job_id, "title": "Engineer", "company": "Acme"}
    base.update(kw)
    return JobListing(**base)


def test_save_jobs_upserts_by_job_id(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "jobs.db"))

    assert storage.save_jobs([_job("1", title="Old Title")]) == 1
    # Same id again => update in place, not a new row.
    assert storage.save_jobs([_job("1", title="New Title"), _job("2")]) == 1

    jobs = {j.job_id: j for j in storage.get_jobs()}
    assert set(jobs) == {"1", "2"}
    assert jobs["1"].title == "New Title"


def test_get_jobs_filters_by_keyword_and_workplace_type(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "jobs.db"))
    storage.save_jobs(
        [
            _job("1", title="Python Developer", workplace_type=WorkplaceType.REMOTE),
            _job("2", title="Java Developer", workplace_type=WorkplaceType.ON_SITE),
        ]
    )

    assert [j.job_id for j in storage.get_jobs(keyword="python")] == ["1"]
    remote = storage.get_jobs(workplace_type=WorkplaceType.REMOTE)
    assert [j.job_id for j in remote] == ["1"]


def test_get_new_jobs_uses_first_seen_at(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "jobs.db"))
    storage.save_jobs([_job("1")])
    cutoff = datetime.now(UTC)
    storage.save_jobs([_job("2")])

    new = storage.get_new_jobs(cutoff)
    assert [j.job_id for j in new] == ["2"]


def test_round_trips_enrichment_fields(tmp_path: Path) -> None:
    storage = Storage(str(tmp_path / "jobs.db"))
    storage.save_jobs(
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
    (job,) = storage.get_jobs()
    assert str(job.company_url) == "https://www.linkedin.com/company/acme"
    assert job.description == "Full description."
    assert job.employment_type == "Full-time"
    assert job.applicant_count == 200


def test_update_preserves_detail_fields_on_search_only_pass(tmp_path: Path) -> None:
    """A later search-only upsert must not wipe earlier detail enrichment."""
    storage = Storage(str(tmp_path / "jobs.db"))
    storage.save_jobs([_job("1", description="Rich detail.", applicant_count=42)])
    # Re-seen via a plain search (no detail fields) — enrichment should survive.
    storage.save_jobs([_job("1", title="Updated Title")])

    (job,) = storage.get_jobs()
    assert job.title == "Updated Title"
    assert job.description == "Rich detail."
    assert job.applicant_count == 42
