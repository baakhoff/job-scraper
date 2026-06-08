"""Pluggable CEO/Founder (people) discovery.

The chosen approach is "search users working at a company by keyword" — but
LinkedIn's public people search is generally **login-gated**, so this is the
one part of the project that can't rely on the proven public jobs endpoints.

To keep that risk contained, discovery sits behind a small ``PeopleProvider``
protocol. The default :class:`NullPeopleProvider` returns nothing (the safe
default); :class:`LinkedInPeopleProvider` makes the best-effort guest scrape.
Either way the rest of the pipeline is unaffected, and a real data source (an
enrichment API, say) can be dropped in later by implementing the protocol.

Use :func:`get_people_provider` to build the configured provider.
"""

from __future__ import annotations

from typing import Protocol

import structlog

from .models import CompanyPerson
from .parser import parse_people_html
from .scraper import LinkedInScraper

log = structlog.get_logger(__name__)


class PeopleProvider(Protocol):
    """Finds people associated with a company, filtered by keyword."""

    name: str

    async def search_people(
        self, company: str, keywords: list[str]
    ) -> list[CompanyPerson]:
        """Return people at ``company`` matching any of ``keywords`` (may be empty)."""
        ...


class NullPeopleProvider:
    """Default provider: finds nothing. Used when people search is disabled.

    Keeps the feature wired end-to-end (the UI shows "no leaders found") without
    making any network request or depending on a fragile/auth-gated source.
    """

    name = "null"

    async def search_people(
        self, company: str, keywords: list[str]
    ) -> list[CompanyPerson]:
        """Always returns an empty list."""
        log.info("people_search_disabled", company=company)
        return []


class LinkedInPeopleProvider:
    """Best-effort people discovery via LinkedIn's (login-gated) people search.

    For each keyword it queries ``"<keyword> <company>"`` and parses any profile
    cards from the response. In practice a logged-out request usually hits an
    auth wall and yields nothing — that's expected and returned as an empty list.
    """

    name = "linkedin"

    def __init__(self, scraper: LinkedInScraper) -> None:
        self._scraper = scraper

    async def search_people(
        self, company: str, keywords: list[str]
    ) -> list[CompanyPerson]:
        """Search ``"<keyword> <company>"`` per keyword; dedupe by profile/name."""
        found: dict[str, CompanyPerson] = {}
        for index, keyword in enumerate(keywords):
            if index > 0:
                await self._scraper.rate_limiter.wait()
            html = await self._scraper.search_people(f"{keyword} {company}")
            if not html.strip():
                continue
            for raw in parse_people_html(html):
                person = CompanyPerson(
                    name=str(raw.get("name") or "").strip(),
                    headline=_opt(raw.get("headline")),
                    profile_url=_opt(raw.get("profile_url")),
                    keyword=keyword,
                    source=self.name,
                )
                if not person.name:
                    continue
                key = str(person.profile_url) if person.profile_url else person.name.lower()
                found.setdefault(key, person)
        people = list(found.values())
        log.info("people_search_done", company=company, found=len(people))
        return people


def _opt(value: object) -> str | None:
    """Coerce a possibly-empty raw value to ``str | None``."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def get_people_provider(scraper: LinkedInScraper | None = None) -> PeopleProvider:
    """Build the provider selected by config.

    Returns :class:`NullPeopleProvider` unless people search is enabled *and*
    ``people_provider == "linkedin"`` *and* a scraper was supplied.
    """
    from config import config

    if (
        config.people_search_enabled
        and config.people_provider == "linkedin"
        and scraper is not None
    ):
        return LinkedInPeopleProvider(scraper)
    return NullPeopleProvider()
