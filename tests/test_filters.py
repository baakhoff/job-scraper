"""Tests for the in-process filtering helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from src.filters import (
    dedupe,
    filter_by_keywords,
    filter_by_workplace_type,
    sort_by_posted_desc,
    tag_workplace_type,
)
from src.models import JobListing, WorkplaceType


def _job(job_id: str, **kw: object) -> JobListing:
    base: dict[str, object] = {"job_id": job_id, "title": "Engineer", "company": "Acme"}
    base.update(kw)
    return JobListing(**base)


def test_filter_by_keywords_include_and_exclude() -> None:
    jobs = [
        _job("1", title="Senior Python Developer"),
        _job("2", title="Java Developer"),
        _job("3", title="Python Intern"),
    ]
    kept = filter_by_keywords(jobs, include=["python"], exclude=["intern"])
    assert [j.job_id for j in kept] == ["1"]


def test_filter_by_workplace_type() -> None:
    jobs = [
        _job("1", workplace_type=WorkplaceType.REMOTE),
        _job("2", workplace_type=WorkplaceType.HYBRID),
        _job("3", workplace_type=None),
    ]
    kept = filter_by_workplace_type(jobs, [WorkplaceType.REMOTE])
    assert [j.job_id for j in kept] == ["1"]


def test_dedupe_keeps_first_seen_order() -> None:
    jobs = [_job("1"), _job("2"), _job("1"), _job("3")]
    assert [j.job_id for j in dedupe(jobs)] == ["1", "2", "3"]


def test_sort_by_posted_desc_pushes_undated_last() -> None:
    jobs = [
        _job("old", posted_at=datetime(2026, 1, 1, tzinfo=UTC)),
        _job("none", posted_at=None),
        _job("new", posted_at=datetime(2026, 6, 1, tzinfo=UTC)),
    ]
    assert [j.job_id for j in sort_by_posted_desc(jobs)] == ["new", "old", "none"]


def test_tag_workplace_type_stamps_only_untagged() -> None:
    untagged = _job("1")  # no workplace_type
    hybrid = _job("2", workplace_type=WorkplaceType.HYBRID)
    tagged = tag_workplace_type([untagged, hybrid], WorkplaceType.REMOTE)
    assert tagged[0].workplace_type is WorkplaceType.REMOTE  # stamped from the search
    assert tagged[1].workplace_type is WorkplaceType.HYBRID  # location-inferred type preserved
    # An unfiltered search (None) changes nothing.
    assert tag_workplace_type([untagged], None)[0].workplace_type is None
