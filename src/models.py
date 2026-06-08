"""Pydantic models for search inputs and parsed job listings.

These are the canonical data shapes that flow through the pipeline:
``SearchParams`` drives the scraper, and ``JobListing`` is what the parser
produces and storage persists.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class WorkplaceType(StrEnum):
    """LinkedIn's workplace-type filter values."""

    ON_SITE = "on_site"
    REMOTE = "remote"
    HYBRID = "hybrid"


class SearchParams(BaseModel):
    """Inputs for a single LinkedIn job search.

    Mirrors the query parameters accepted by the public guest jobs endpoint
    (keywords, location, geo id, time filters, pagination offset).
    """

    keywords: str = Field(..., description="Free-text search, e.g. 'python backend'.")
    location: str | None = Field(None, description="Human-readable location string.")
    geo_id: str | None = Field(None, description="LinkedIn geoId; more reliable than location.")
    workplace_type: WorkplaceType | None = Field(
        None, description="Filter by on-site / remote / hybrid."
    )
    posted_within_seconds: int | None = Field(
        None, description="Only jobs posted within the last N seconds (LinkedIn 'f_TPR')."
    )
    start: int = Field(0, ge=0, description="Pagination offset; page size is 25.")

    def to_query(self) -> dict[str, str]:
        """Serialize to the query-param dict expected by the guest endpoint.

        Stub: real implementation maps fields to LinkedIn's parameter names
        (``keywords``, ``location``, ``geoId``, ``f_WT``, ``f_TPR``, ``start``).
        """
        raise NotImplementedError


class JobListing(BaseModel):
    """A single parsed job posting."""

    job_id: str = Field(..., description="LinkedIn's numeric job id (stable key).")
    title: str
    company: str
    location: str | None = None
    workplace_type: WorkplaceType | None = None
    url: HttpUrl | None = None
    posted_at: datetime | None = None
    description: str | None = Field(None, description="Full description, when the detail page is fetched.")
    salary: str | None = None
    seniority: str | None = None

    @classmethod
    def from_raw(cls, raw: dict[str, object]) -> JobListing:
        """Build a validated ``JobListing`` from a parser raw dict.

        Stub: normalize/clean the loosely-typed dict the parser emits.
        """
        raise NotImplementedError
