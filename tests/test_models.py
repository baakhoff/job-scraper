"""Tests for model normalization and query mapping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from src.models import JobListing, SearchParams, WorkplaceType


def test_to_query_maps_fields_to_linkedin_param_names() -> None:
    params = SearchParams(
        keywords="python backend",
        location="Berlin",
        geo_id="123",
        workplace_type=WorkplaceType.REMOTE,
        posted_within_seconds=86400,
        start=25,
    )
    query = params.to_query()
    assert query == {
        "keywords": "python backend",
        "location": "Berlin",
        "geoId": "123",
        "f_WT": "2",
        "f_TPR": "r86400",
        "start": "25",
    }


def test_to_query_omits_empty_optional_fields() -> None:
    query = SearchParams(keywords="python").to_query()
    assert query == {"keywords": "python", "start": "0"}


def test_from_raw_infers_remote_and_parses_iso_date() -> None:
    job = JobListing.from_raw(
        {
            "job_id": "  42 ",
            "title": "  Python   Developer ",
            "company": "Acme",
            "location": "Berlin, Germany (Remote)",
            "url": "https://example.com/jobs/view/42",
            "posted_at": "2026-06-01",
        }
    )
    assert job.job_id == "42"
    assert job.title == "Python Developer"  # whitespace collapsed
    assert job.workplace_type is WorkplaceType.REMOTE
    assert job.posted_at == datetime(2026, 6, 1, tzinfo=UTC)


def test_from_raw_parses_relative_posted_text() -> None:
    job = JobListing.from_raw(
        {"job_id": "1", "title": "T", "company": "C", "posted_text": "2 weeks ago"}
    )
    assert job.posted_at is not None
    delta = datetime.now(UTC) - job.posted_at
    assert timedelta(days=13) < delta < timedelta(days=15)


def test_from_raw_rejects_empty_title() -> None:
    with pytest.raises(ValidationError):
        JobListing.from_raw({"job_id": "1", "title": "   ", "company": "C"})


def test_from_raw_reads_company_url_and_detail_fields() -> None:
    job = JobListing.from_raw(
        {
            "job_id": "1",
            "title": "Engineer",
            "company": "Acme",
            "company_url": "https://www.linkedin.com/company/acme",
            "description": "Full description here.",
            "seniority": "Mid-Senior level",
            "employment_type": "Full-time",
            "job_function": "Engineering",
            "industries": "Software Development",
            "applicant_count": "Over 200",
        }
    )
    assert str(job.company_url) == "https://www.linkedin.com/company/acme"
    assert job.description == "Full description here."
    assert job.employment_type == "Full-time"
    assert job.applicant_count == 200  # coerced from "Over 200"
