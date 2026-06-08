"""Post-fetch filtering of parsed job listings.

The scraper/parser pull a broad result set; these filters narrow it down
in-process (LinkedIn's own filters are coarse and unreliable). Each filter
is a small predicate-style helper that takes listings and returns a subset.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from .models import JobListing, WorkplaceType


def filter_by_keywords(
    listings: Iterable[JobListing],
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[JobListing]:
    """Keep listings whose title/company match include and avoid exclude terms.

    Case-insensitive substring matching across title, company, and description
    snippet. ``include`` is AND-style (all terms must appear); ``exclude`` drops
    a listing if any term appears.
    """
    include_terms = [t.lower() for t in include if t]
    exclude_terms = [t.lower() for t in exclude if t]

    kept: list[JobListing] = []
    for listing in listings:
        haystack = " ".join(
            part.lower()
            for part in (listing.title, listing.company, listing.description_snippet)
            if part
        )
        if any(term in haystack for term in exclude_terms):
            continue
        if all(term in haystack for term in include_terms):
            kept.append(listing)
    return kept


def filter_by_workplace_type(
    listings: Iterable[JobListing], allowed: Iterable[WorkplaceType]
) -> list[JobListing]:
    """Keep only listings whose workplace type is in ``allowed``."""
    allowed_set = set(allowed)
    if not allowed_set:
        return list(listings)
    return [listing for listing in listings if listing.workplace_type in allowed_set]


def dedupe(listings: Iterable[JobListing]) -> list[JobListing]:
    """Remove duplicate listings, keyed by ``job_id`` (preserves first-seen order)."""
    seen: set[str] = set()
    unique: list[JobListing] = []
    for listing in listings:
        if listing.job_id in seen:
            continue
        seen.add(listing.job_id)
        unique.append(listing)
    return unique


def sort_by_posted_desc(listings: Iterable[JobListing]) -> list[JobListing]:
    """Sort newest-first by ``posted_at``; listings without a date sort last."""
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(listings, key=lambda job: job.posted_at or epoch, reverse=True)
