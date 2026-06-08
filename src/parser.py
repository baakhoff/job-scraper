"""Parse raw LinkedIn job-card HTML into loosely-typed dicts.

The guest endpoint returns a flat list of ``<li>`` job cards. This module
extracts fields with BeautifulSoup and emits plain dicts; validation and
normalization happen later in ``models.JobListing.from_raw``.

NOTE: the selectors here are inherently fragile — LinkedIn changes class
names and DOM structure without notice. Keep selectors centralized so they
are easy to repair when the markup shifts.
"""

from __future__ import annotations

import re

import structlog
from bs4 import BeautifulSoup
from bs4.element import Tag

log = structlog.get_logger(__name__)

# Centralized, fragile selectors — update these when LinkedIn changes markup.
CARD_SELECTOR = "li"
TITLE_SELECTOR = "h3.base-search-card__title"
COMPANY_SELECTOR = "h4.base-search-card__subtitle"
LOCATION_SELECTOR = "span.job-search-card__location"
LINK_SELECTOR = "a.base-card__full-link"
TIME_SELECTOR = "time"
SNIPPET_SELECTOR = "p.job-search-card__snippet"
# The card root carries the job id in a data attribute; a couple of variants
# have shipped over time, so try each.
ENTITY_URN_ATTRS = ("data-entity-urn", "data-id")
URN_JOB_ID_RE = re.compile(r"(\d{6,})")


def parse_search_html(html: str) -> list[dict[str, object]]:
    """Parse a page of job-card HTML into a list of raw dicts.

    Builds a ``BeautifulSoup`` tree, selects cards via ``CARD_SELECTOR``, and
    runs :func:`parse_card` on each. Cards that fail to parse (or have no job
    id) are skipped rather than raised — parse failures are expected
    operational events when LinkedIn ships markup changes.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(CARD_SELECTOR)
    results: list[dict[str, object]] = []
    for card in cards:
        if not isinstance(card, Tag):
            continue
        raw = parse_card(card)
        if raw is not None:
            results.append(raw)
    return results


def parse_card(card: Tag) -> dict[str, object] | None:
    """Extract a single job card's fields into a raw dict.

    Returns ``None`` if the card has no resolvable job id (e.g. it's a
    structural ``<li>`` rather than a real job card). All other fields are
    best-effort and may be missing.
    """
    job_id = _extract_job_id(card)
    if not job_id:
        return None

    title = _text(card, TITLE_SELECTOR)
    company = _text(card, COMPANY_SELECTOR)
    if not title or not company:
        log.debug("card_missing_core_fields", job_id=job_id, has_title=bool(title))
        return None

    time_tag = card.select_one(TIME_SELECTOR)
    posted_at = time_tag.get("datetime") if isinstance(time_tag, Tag) else None
    posted_text = time_tag.get_text(strip=True) if isinstance(time_tag, Tag) else None

    return {
        "job_id": job_id,
        "title": title,
        "company": company,
        "location": _text(card, LOCATION_SELECTOR),
        "url": _href(card, LINK_SELECTOR),
        "posted_at": posted_at,
        "posted_text": posted_text,
        "description_snippet": _text(card, SNIPPET_SELECTOR),
    }


def parse_detail_html(html: str) -> dict[str, object]:
    """Parse a single job *detail* page (full description, salary, seniority).

    Used when a deeper per-job fetch is enabled.
    """
    soup = BeautifulSoup(html, "lxml")
    description_el = soup.select_one("div.show-more-less-html__markup, div.description__text")
    criteria: dict[str, str] = {}
    for item in soup.select("li.description__job-criteria-item"):
        header = item.select_one("h3.description__job-criteria-subheader")
        value = item.select_one("span.description__job-criteria-text")
        if isinstance(header, Tag) and isinstance(value, Tag):
            criteria[header.get_text(strip=True).lower()] = value.get_text(strip=True)

    return {
        "description": description_el.get_text(" ", strip=True) if description_el else None,
        "seniority": criteria.get("seniority level"),
        "salary": criteria.get("base salary"),
    }


def _extract_job_id(card: Tag) -> str | None:
    """Pull the numeric job id from the card's data attributes or links."""
    for attr in ENTITY_URN_ATTRS:
        value = card.get(attr)
        if isinstance(value, str):
            match = URN_JOB_ID_RE.search(value)
            if match:
                return match.group(1)

    # Fallback: dig the id out of the canonical job link, e.g.
    # .../jobs/view/python-developer-at-acme-3811234567
    link = card.select_one(LINK_SELECTOR)
    if isinstance(link, Tag):
        href = link.get("href")
        if isinstance(href, str):
            match = re.search(r"/jobs/view/[^/?]*?(\d{6,})", href)
            if match:
                return match.group(1)
    return None


def _text(card: Tag, selector: str) -> str | None:
    """Return stripped text for the first match of ``selector``, or ``None``."""
    el = card.select_one(selector)
    if not isinstance(el, Tag):
        return None
    text = el.get_text(strip=True)
    return text or None


def _href(card: Tag, selector: str) -> str | None:
    """Return the ``href`` of the first match of ``selector``, trimmed of query."""
    el = card.select_one(selector)
    if not isinstance(el, Tag):
        return None
    href = el.get("href")
    if not isinstance(href, str):
        return None
    # Drop tracking query params for a stable, clean URL.
    return href.split("?", 1)[0] or None
