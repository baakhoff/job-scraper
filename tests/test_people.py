"""Tests for the pluggable people (CEO/Founder) providers (offline)."""

from __future__ import annotations

import httpx
import pytest

from src.people import (
    LinkedInPeopleProvider,
    NullPeopleProvider,
    get_people_provider,
)
from src.scraper import LinkedInScraper, RateLimiter

_FAST = RateLimiter(0.0, 0.0, backoff_base=0.0, backoff_ceiling=0.0)


def _scraper(html: str, status: int = 200) -> LinkedInScraper:
    """A scraper whose every request returns the given fixture HTML/status."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text=html)

    scraper = LinkedInScraper(rate_limiter=_FAST, user_agents=["test-agent"])
    scraper._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return scraper


_PEOPLE_HTML = """
<li>
  <a href="https://www.linkedin.com/in/jane-doe">Jane Doe</a>
  <p class="entity-result__primary-subtitle">CEO at Acme</p>
</li>
"""


async def test_null_provider_returns_empty() -> None:
    assert await NullPeopleProvider().search_people("Acme", ["CEO"]) == []


async def test_linkedin_provider_parses_and_tags_people() -> None:
    async with _scraper(_PEOPLE_HTML) as scraper:
        people = await LinkedInPeopleProvider(scraper).search_people("Acme", ["CEO"])
    assert [p.name for p in people] == ["Jane Doe"]
    assert people[0].keyword == "CEO"
    assert people[0].source == "linkedin"
    assert str(people[0].profile_url) == "https://www.linkedin.com/in/jane-doe"


async def test_linkedin_provider_empty_on_authwall() -> None:
    async with _scraper("Sign in to continue", status=999) as scraper:
        assert await LinkedInPeopleProvider(scraper).search_people("Acme", ["CEO"]) == []


def test_get_people_provider_defaults_to_null() -> None:
    assert get_people_provider(None).name == "null"


def test_get_people_provider_selects_linkedin_when_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from config import config

    monkeypatch.setattr(config, "people_search_enabled", True)
    monkeypatch.setattr(config, "people_provider", "linkedin")
    provider = get_people_provider(_scraper(""))
    assert provider.name == "linkedin"
