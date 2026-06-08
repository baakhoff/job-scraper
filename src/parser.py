"""Parse raw LinkedIn job-card HTML into loosely-typed dicts.

The guest endpoint returns a flat list of ``<li>`` job cards. This module
extracts fields with BeautifulSoup and emits plain dicts; validation and
normalization happen later in ``models.JobListing.from_raw``.

NOTE: the selectors here are inherently fragile — LinkedIn changes class
names and DOM structure without notice. Keep selectors centralized so they
are easy to repair when the markup shifts.
"""

from __future__ import annotations

from bs4 import BeautifulSoup
from bs4.element import Tag

# Centralized, fragile selectors — update these when LinkedIn changes markup.
CARD_SELECTOR = "li"
TITLE_SELECTOR = "h3.base-search-card__title"
COMPANY_SELECTOR = "h4.base-search-card__subtitle"
LOCATION_SELECTOR = "span.job-search-card__location"
LINK_SELECTOR = "a.base-card__full-link"
TIME_SELECTOR = "time"


def parse_search_html(html: str) -> list[dict[str, object]]:
    """Parse a page of job-card HTML into a list of raw dicts.

    Stub: build a ``BeautifulSoup`` tree, select cards via ``CARD_SELECTOR``,
    and call :func:`parse_card` on each.
    """
    soup = BeautifulSoup(html, "lxml")
    cards = soup.select(CARD_SELECTOR)
    return [parse_card(card) for card in cards if isinstance(card, Tag)]


def parse_card(card: Tag) -> dict[str, object]:
    """Extract a single job card's fields into a raw dict.

    Stub: pull title, company, location, url, job id, and posted time from the
    card using the module-level selectors. Returns loosely-typed values for
    ``models.JobListing.from_raw`` to clean up.
    """
    raise NotImplementedError


def parse_detail_html(html: str) -> dict[str, object]:
    """Parse a single job *detail* page (full description, salary, seniority).

    Stub: used when a deeper fetch per job is enabled.
    """
    raise NotImplementedError
