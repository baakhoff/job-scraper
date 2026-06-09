"""Parse raw LinkedIn job-card HTML into loosely-typed dicts.

The guest endpoint returns a flat list of ``<li>`` job cards. This module
extracts fields with BeautifulSoup and emits plain dicts; validation and
normalization happen later in ``models.JobListing.from_raw``.

NOTE: the selectors here are inherently fragile — LinkedIn changes class
names and DOM structure without notice. Keep selectors centralized so they
are easy to repair when the markup shifts.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator

import structlog
from bs4 import BeautifulSoup
from bs4.element import Tag

log = structlog.get_logger(__name__)

# Centralized, fragile selectors — update these when LinkedIn changes markup.
CARD_SELECTOR = "li"
TITLE_SELECTOR = "h3.base-search-card__title"
COMPANY_SELECTOR = "h4.base-search-card__subtitle"
# The company name in the subtitle wraps an anchor to the public company
# profile (/company/...). Free on every search card — no detail fetch needed.
COMPANY_LINK_SELECTOR = "h4.base-search-card__subtitle a"
LOCATION_SELECTOR = "span.job-search-card__location"
LINK_SELECTOR = "a.base-card__full-link"
TIME_SELECTOR = "time"
SNIPPET_SELECTOR = "p.job-search-card__snippet"

# Detail-page selectors (jobs-guest/.../jobPosting/{id}). Also fragile.
DETAIL_DESCRIPTION_SELECTOR = "div.show-more-less-html__markup, div.description__text"
DETAIL_CRITERIA_ITEM_SELECTOR = "li.description__job-criteria-item"
DETAIL_CRITERIA_HEADER_SELECTOR = "h3.description__job-criteria-subheader"
DETAIL_CRITERIA_VALUE_SELECTOR = "span.description__job-criteria-text"
DETAIL_COMPANY_LINK_SELECTOR = "a.topcard__org-name-link"
DETAIL_APPLICANTS_RE = re.compile(r"([\d,]+)\s+applicants?", re.IGNORECASE)

# Company "about" page selectors (linkedin.com/company/{slug}). Logged-out
# company pages are sparse and often fall back to OpenGraph <meta> tags, so we
# read those first and treat the visible DOM as a best-effort supplement.
COMPANY_NAME_SELECTOR = "h1.top-card-layout__title, h1.org-top-card-summary__title"
COMPANY_DESC_SELECTOR = (
    "p.about-us__description, section.about-us p, div.core-section-container__content p"
)
# The about page renders facts as <dt>label</dt><dd>value</dd> pairs.
COMPANY_DEFINITION_SELECTOR = "div.about-us__basic-info-container dl, section.about-us dl"

# People search/result cards (best-effort; public people search is login-gated,
# so this also handles generic profile-link result pages). Any anchor to a
# /in/{slug} profile is treated as a person card.
PEOPLE_PROFILE_LINK_SELECTOR = "a[href*='/in/']"
PEOPLE_HEADLINE_SELECTOR = (
    "p.people-search-card__headline, p.entity-result__primary-subtitle, "
    "div.subline-level-1"
)
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
        "company_url": _href(card, COMPANY_LINK_SELECTOR),
        "location": _text(card, LOCATION_SELECTOR),
        "url": _href(card, LINK_SELECTOR),
        "posted_at": posted_at,
        "posted_text": posted_text,
        "description_snippet": _text(card, SNIPPET_SELECTOR),
    }


def parse_detail_html(html: str) -> dict[str, object]:
    """Parse a single job *detail* page into a raw dict of enrichment fields.

    The guest detail fragment (``jobs-guest/.../jobPosting/{id}``) exposes the
    full description plus a "job criteria" block (seniority, employment type,
    job function, industries), an applicant count, and the company profile
    link. Used when a deeper per-job fetch is enabled (``search --details``).

    All fields are best-effort; missing ones come back as ``None``. Merge the
    result into a search-card raw dict before :meth:`JobListing.from_raw`.
    """
    soup = BeautifulSoup(html, "lxml")
    description_el = soup.select_one(DETAIL_DESCRIPTION_SELECTOR)
    criteria: dict[str, str] = {}
    for item in soup.select(DETAIL_CRITERIA_ITEM_SELECTOR):
        header = item.select_one(DETAIL_CRITERIA_HEADER_SELECTOR)
        value = item.select_one(DETAIL_CRITERIA_VALUE_SELECTOR)
        if isinstance(header, Tag) and isinstance(value, Tag):
            criteria[header.get_text(strip=True).lower()] = value.get_text(strip=True)

    applicants_match = DETAIL_APPLICANTS_RE.search(html)
    applicant_count = (
        int(applicants_match.group(1).replace(",", "")) if applicants_match else None
    )

    return {
        "description": description_el.get_text(" ", strip=True) if description_el else None,
        "seniority": criteria.get("seniority level"),
        "salary": criteria.get("base salary") or criteria.get("compensation"),
        "employment_type": criteria.get("employment type"),
        "job_function": criteria.get("job function"),
        "industries": criteria.get("industries"),
        "applicant_count": applicant_count,
        "company_url": _href_soup(soup, DETAIL_COMPANY_LINK_SELECTOR),
    }


def _iter_jsonld_orgs(data: object) -> Iterator[dict[str, object]]:
    """Yield every Organization-typed node from a parsed JSON-LD document.

    Handles the three shapes LinkedIn emits: a bare object, a list of objects,
    or a ``{"@graph": [...]}`` wrapper. A node counts as an Organization if its
    ``@type`` mentions "Organization".
    """
    if isinstance(data, list):
        for item in data:
            yield from _iter_jsonld_orgs(item)
        return
    if not isinstance(data, dict):
        return
    graph = data.get("@graph")
    if isinstance(graph, list):
        for item in graph:
            yield from _iter_jsonld_orgs(item)
    raw_type = data.get("@type")
    types = raw_type if isinstance(raw_type, list) else [raw_type]
    if any(isinstance(t, str) and "Organization" in t for t in types):
        yield data


def _company_jsonld(soup: BeautifulSoup) -> dict[str, str]:
    """Extract company fields from any Organization JSON-LD block.

    LinkedIn company pages embed a schema.org Organization as JSON-LD, which is
    far more stable than the visible DOM. Returns a partial dict with keys among
    ``name`` / ``description`` / ``website`` / ``headquarters``; a missing or
    malformed block yields ``{}`` (callers fall back to OpenGraph + DOM).
    """
    out: dict[str, str] = {}
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        if not isinstance(script, Tag):
            continue
        raw = script.string or script.get_text()
        if not raw or not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except (ValueError, TypeError):
            continue
        for node in _iter_jsonld_orgs(data):
            name = node.get("name")
            if isinstance(name, str) and name.strip() and "name" not in out:
                out["name"] = name.strip()
            desc = node.get("description")
            if isinstance(desc, str) and desc.strip() and "description" not in out:
                out["description"] = desc.strip()
            # Website: the first non-LinkedIn http(s) URL in ``sameAs`` (``url``
            # is the LinkedIn page itself, so it is not a useful website signal).
            same = node.get("sameAs")
            candidates = same if isinstance(same, list) else [same]
            for candidate in candidates:
                if (
                    isinstance(candidate, str)
                    and candidate.startswith(("http://", "https://"))
                    and "linkedin.com" not in candidate
                ):
                    out.setdefault("website", candidate)
                    break
            addr = node.get("address")
            if "headquarters" not in out:
                if isinstance(addr, dict):
                    parts = [addr.get("addressLocality"), addr.get("addressRegion"),
                             addr.get("addressCountry")]
                    hq = ", ".join(p for p in parts if isinstance(p, str) and p)
                    if hq:
                        out["headquarters"] = hq
                elif isinstance(addr, str) and addr.strip():
                    out["headquarters"] = addr.strip()
    return out


def parse_company_html(html: str) -> dict[str, object]:
    """Parse a public company page into a best-effort enrichment dict.

    Logged-out company pages are sparse and version-dependent, so this reads the
    embedded Organization **JSON-LD** first (the most stable signal), then
    OpenGraph ``<meta>`` tags, then visible DOM. Returns ``{name, description,
    industry, company_size, website, headquarters}`` with ``None`` for anything
    missing.

    NOTE: like the job selectors, these are fragile and may need updating when
    LinkedIn ships markup changes; missing fields are expected, not errors.
    """
    soup = BeautifulSoup(html, "lxml")
    ld = _company_jsonld(soup)

    name = ld.get("name") or _meta(soup, "og:title") or _text_soup(soup, COMPANY_NAME_SELECTOR)
    description = (
        ld.get("description")
        or _meta(soup, "og:description")
        or _text_soup(soup, COMPANY_DESC_SELECTOR)
    )

    # Pull label -> value pairs out of any about-page definition list.
    facts: dict[str, str] = {}
    for dl in soup.select(COMPANY_DEFINITION_SELECTOR):
        if not isinstance(dl, Tag):
            continue
        terms = dl.find_all("dt")
        values = dl.find_all("dd")
        for term, value in zip(terms, values, strict=False):
            if isinstance(term, Tag) and isinstance(value, Tag):
                facts[term.get_text(strip=True).lower()] = value.get_text(" ", strip=True)

    return {
        "name": name,
        "description": description,
        "industry": facts.get("industry") or facts.get("industries"),
        "company_size": facts.get("company size") or facts.get("size"),
        "website": ld.get("website") or facts.get("website") or _meta(soup, "og:url"),
        "headquarters": ld.get("headquarters") or facts.get("headquarters"),
    }


def parse_people_html(html: str) -> list[dict[str, object]]:
    """Parse a people-results page into a list of ``{name, headline, profile_url}``.

    Best-effort and deliberately loose: any anchor pointing at a ``/in/{slug}``
    profile is treated as a person card, deduped by profile URL. Public people
    search is generally login-gated, so on a guest page this often returns an
    empty list — which the caller treats as "no leaders found", not an error.
    """
    soup = BeautifulSoup(html, "lxml")
    people: list[dict[str, object]] = []
    seen: set[str] = set()
    for link in soup.select(PEOPLE_PROFILE_LINK_SELECTOR):
        if not isinstance(link, Tag):
            continue
        href = link.get("href")
        if not isinstance(href, str) or "/in/" not in href:
            continue
        profile_url = href.split("?", 1)[0]
        if profile_url in seen:
            continue
        name = link.get_text(strip=True)
        if not name:
            continue
        seen.add(profile_url)
        # The headline usually sits in a sibling/parent subtitle element.
        headline = None
        container = link.find_parent(["li", "div"])
        if isinstance(container, Tag):
            headline = _text(container, PEOPLE_HEADLINE_SELECTOR)
        people.append({"name": name, "headline": headline, "profile_url": profile_url})
    return people


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    """Return the ``content`` of an OpenGraph/standard ``<meta>`` tag, or ``None``."""
    el = soup.find("meta", attrs={"property": prop}) or soup.find("meta", attrs={"name": prop})
    if not isinstance(el, Tag):
        return None
    content = el.get("content")
    return content.strip() if isinstance(content, str) and content.strip() else None


def _text_soup(soup: BeautifulSoup, selector: str) -> str | None:
    """``_text`` against a whole-document soup (pages, not a single card)."""
    el = soup.select_one(selector)
    if not isinstance(el, Tag):
        return None
    text = el.get_text(" ", strip=True)
    return text or None


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


def _href_soup(soup: BeautifulSoup, selector: str) -> str | None:
    """``_href`` against a whole-document soup (detail pages, not a single card)."""
    el = soup.select_one(selector)
    if not isinstance(el, Tag):
        return None
    href = el.get("href")
    if not isinstance(href, str):
        return None
    return href.split("?", 1)[0] or None
