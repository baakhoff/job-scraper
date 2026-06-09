"""Pydantic models for search inputs and parsed job listings.

These are the canonical data shapes that flow through the pipeline:
``SearchParams`` drives the scraper, and ``JobListing`` is what the parser
produces and storage persists.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl, field_validator

from .language import accept_language_header, detect_language

# LinkedIn's ``f_WT`` workplace-type filter codes.
_WORKPLACE_FILTER_CODES: dict[str, str] = {
    "on_site": "1",
    "remote": "2",
    "hybrid": "3",
}


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
    language: str | None = Field(
        None, description="ISO code (e.g. 'de') hinting LinkedIn's locale via Accept-Language."
    )
    start: int = Field(0, ge=0, description="Pagination offset; page size is 25.")

    def accept_language_header(self) -> str | None:
        """The ``Accept-Language`` header value for :attr:`language`, or ``None``."""
        return accept_language_header(self.language)

    def to_query(self) -> dict[str, str]:
        """Serialize to the query-param dict expected by the guest endpoint.

        Maps our fields onto LinkedIn's parameter names. Only non-empty fields
        are included so the URL stays minimal.
        """
        query: dict[str, str] = {"keywords": self.keywords, "start": str(self.start)}
        if self.location:
            query["location"] = self.location
        if self.geo_id:
            query["geoId"] = self.geo_id
        if self.workplace_type is not None:
            query["f_WT"] = _WORKPLACE_FILTER_CODES[self.workplace_type.value]
        if self.posted_within_seconds is not None:
            query["f_TPR"] = f"r{self.posted_within_seconds}"
        return query


class JobListing(BaseModel):
    """A single parsed job posting."""

    job_id: str = Field(..., description="LinkedIn's numeric job id (stable key).")
    title: str
    company: str
    company_url: HttpUrl | None = Field(
        None, description="Public company profile URL (from the search card subtitle link)."
    )
    location: str | None = None
    workplace_type: WorkplaceType | None = None
    url: HttpUrl | None = None
    posted_at: datetime | None = None
    description: str | None = Field(
        None, description="Full description, when the detail page is fetched."
    )
    description_snippet: str | None = None
    salary: str | None = None
    seniority: str | None = Field(None, description="e.g. 'Mid-Senior level' (detail page).")
    employment_type: str | None = Field(None, description="e.g. 'Full-time' (detail page).")
    job_function: str | None = Field(
        None, description="e.g. 'Engineering and Information Technology' (detail page)."
    )
    industries: str | None = Field(None, description="e.g. 'Software Development' (detail page).")
    applicant_count: int | None = Field(
        None, description="Applicant count parsed from the detail page, when shown."
    )
    language: str | None = Field(
        None, description="Best-effort detected ISO language code (heuristic; may be None)."
    )

    @field_validator("title", "company", mode="before")
    @classmethod
    def _require_text(cls, value: object) -> str:
        """Collapse whitespace; reject empty required text fields."""
        text = _clean_text(value)
        if not text:
            raise ValueError("required text field is empty")
        return text

    @classmethod
    def from_raw(cls, raw: dict[str, object]) -> JobListing:
        """Build a validated ``JobListing`` from a parser raw dict.

        Normalizes the loosely-typed dict the parser emits: trims whitespace,
        infers workplace type from the location string, and parses the
        relative/absolute posted time into a UTC ``datetime``.

        Optional detail-page fields (``description``, ``employment_type``,
        ``applicant_count`` …) are read when present, so a search-card dict that
        has been merged with :func:`~src.parser.parse_detail_html` output
        produces a fully-populated listing.
        """
        location = _clean_text(raw.get("location")) or None
        title = _clean_text(raw.get("title"))
        snippet = _clean_text(raw.get("description_snippet")) or None
        description = _clean_text(raw.get("description")) or None
        return cls(
            job_id=str(raw.get("job_id") or "").strip(),
            title=raw.get("title"),
            company=raw.get("company"),
            company_url=_clean_text(raw.get("company_url")) or None,
            location=location,
            workplace_type=_infer_workplace_type(location),
            url=_clean_text(raw.get("url")) or None,
            posted_at=_parse_posted_at(raw.get("posted_at"), raw.get("posted_text")),
            description=description,
            description_snippet=snippet,
            salary=_clean_text(raw.get("salary")) or None,
            seniority=_clean_text(raw.get("seniority")) or None,
            employment_type=_clean_text(raw.get("employment_type")) or None,
            job_function=_clean_text(raw.get("job_function")) or None,
            industries=_clean_text(raw.get("industries")) or None,
            applicant_count=_coerce_int(raw.get("applicant_count")),
            # Detect from the richest text available (full description when the
            # detail page was fetched, else title + snippet). Heuristic → may be None.
            language=detect_language(title, snippet, description),
        )


class Position(BaseModel):
    """A role/keyword the user has searched for (e.g. 'python developer').

    A position groups the listings found for that search and, through them, the
    companies hiring for it. ``id`` is assigned by the database.
    """

    id: int | None = None
    keyword: str = Field(..., description="The search term, as displayed.")
    location: str | None = None
    company_count: int = Field(0, description="Distinct companies hiring for this position.")
    listing_count: int = Field(0, description="Listings stored for this position.")


class Company(BaseModel):
    """A company aggregated from job listings, optionally enriched.

    The core fields (``name``, ``company_url``) come free from every search
    card; the rest are filled in best-effort by a company-page enrichment pass.
    """

    id: int | None = None
    name: str
    company_url: HttpUrl | None = None
    slug: str | None = Field(None, description="The '/company/{slug}' handle, when known.")
    location: str | None = None
    industry: str | None = None
    company_size: str | None = None
    website: str | None = None
    description: str | None = None
    listing_count: int = Field(0, description="Open listings stored for this company.")


class CompanyPerson(BaseModel):
    """A person associated with a company — e.g. a CEO or founder.

    Discovered by searching people who work at the company by keyword. The
    ``keyword`` records which term surfaced them ('CEO', 'Founder', …).
    """

    id: int | None = None
    name: str
    headline: str | None = Field(None, description="Their LinkedIn headline / title line.")
    profile_url: HttpUrl | None = None
    keyword: str | None = None
    source: str | None = Field(None, description="Which provider produced this record.")


def company_slug(url: object) -> str | None:
    """Extract the lowercased ``/company/{slug}`` handle from a LinkedIn URL."""
    if not url:
        return None
    match = re.search(r"/company/([^/?#]+)", str(url))
    return match.group(1).lower() if match else None


def normalize_company_name(name: object) -> str | None:
    """Lowercase + collapse whitespace, for name-based company de-duplication."""
    cleaned = _clean_text(name)
    return cleaned.lower() or None


def _clean_text(value: object) -> str:
    """Collapse runs of whitespace and strip; ``None`` becomes ``""``."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def _coerce_int(value: object) -> int | None:
    """Best-effort int from an int or a string like '1,234' / 'Over 200'."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    match = re.search(r"\d[\d,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def _infer_workplace_type(location: str | None) -> WorkplaceType | None:
    """Best-effort workplace type from the card's location text.

    LinkedIn often appends "(Remote)" / "(Hybrid)" to the location on guest
    cards; there is no dedicated field, so we sniff the string.
    """
    if not location:
        return None
    lowered = location.lower()
    if "remote" in lowered:
        return WorkplaceType.REMOTE
    if "hybrid" in lowered:
        return WorkplaceType.HYBRID
    if "on-site" in lowered or "on site" in lowered:
        return WorkplaceType.ON_SITE
    return None


# Maps the unit word in "3 hours ago" to a timedelta-kwarg builder.
_RELATIVE_UNITS: dict[str, str] = {
    "second": "seconds",
    "minute": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
}


def _parse_posted_at(iso_value: object, relative_text: object) -> datetime | None:
    """Resolve a posting timestamp.

    Prefers the machine-readable ``datetime`` attribute on the card's ``<time>``
    tag (an ISO date). Falls back to parsing human strings like
    "2 weeks ago" relative to *now*.
    """
    now = datetime.now(UTC)

    iso = _clean_text(iso_value)
    if iso:
        try:
            parsed = datetime.fromisoformat(iso)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed

    text = _clean_text(relative_text).lower()
    match = re.search(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", text)
    if match:
        amount = int(match.group(1))
        unit = match.group(2)
        if unit == "month":
            return now - timedelta(days=30 * amount)
        if unit == "year":
            return now - timedelta(days=365 * amount)
        return now - timedelta(**{_RELATIVE_UNITS[unit]: amount})
    return None
