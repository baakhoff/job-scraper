"""Post-fetch filtering of parsed job listings.

The scraper/parser pull a broad result set; these filters narrow it down
in-process (LinkedIn's own filters are coarse and unreliable). Each filter
is a small predicate-style helper that takes listings and returns a subset.
"""

from __future__ import annotations

from collections.abc import Iterable

from .models import JobListing, WorkplaceType


def filter_by_keywords(
    listings: Iterable[JobListing],
    include: Iterable[str] = (),
    exclude: Iterable[str] = (),
) -> list[JobListing]:
    """Keep listings whose title/description match include and avoid exclude terms.

    Stub: case-insensitive substring matching across title and description.
    """
    raise NotImplementedError


def filter_by_workplace_type(
    listings: Iterable[JobListing], allowed: Iterable[WorkplaceType]
) -> list[JobListing]:
    """Keep only listings whose workplace type is in ``allowed``.

    Stub.
    """
    raise NotImplementedError


def dedupe(listings: Iterable[JobListing]) -> list[JobListing]:
    """Remove duplicate listings, keyed by ``job_id``.

    Stub: preserves first-seen order.
    """
    raise NotImplementedError
