"""Async HTTP scraper for the public LinkedIn jobs endpoint.

Targets the *guest* job-search API (no login required):

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

which returns a chunk of raw HTML job cards. Pagination is offset-based via
the ``start`` parameter (page size 25). Rate limiting is essential — LinkedIn
will return 429 / soft-block aggressively under load.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import httpx

from .models import SearchParams

GUEST_SEARCH_URL = (
    "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
)
PAGE_SIZE = 25


class RateLimiter:
    """Simple async delay gate between requests.

    Stub: a real implementation should support jittered delays and back off
    on 429 responses (exponential backoff with a ceiling).
    """

    def __init__(self, delay_seconds: float, jitter_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.jitter_seconds = jitter_seconds

    async def wait(self) -> None:
        """Sleep for the configured delay (plus jitter) before the next request."""
        # Stub: jitter omitted; replace with randomized backoff-aware delay.
        await asyncio.sleep(self.delay_seconds)


class LinkedInScraper:
    """Fetches raw job-card HTML pages from the guest endpoint.

    Owns an ``httpx.AsyncClient`` and a ``RateLimiter``. Yields raw HTML
    strings; turning HTML into dicts is the parser's job.
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        user_agent: str,
        timeout: float = 10.0,
        max_pages: int = 40,
    ) -> None:
        self.rate_limiter = rate_limiter
        self.max_pages = max_pages
        self._client = httpx.AsyncClient(
            headers={"User-Agent": user_agent},
            timeout=timeout,
            follow_redirects=True,
        )

    async def __aenter__(self) -> LinkedInScraper:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    async def fetch_page(self, params: SearchParams) -> str:
        """Fetch a single page of job-card HTML for the given params.

        Stub: issue the GET against ``GUEST_SEARCH_URL`` with
        ``params.to_query()``, raise for status, and return ``response.text``.
        """
        raise NotImplementedError

    async def iter_pages(self, params: SearchParams) -> AsyncIterator[str]:
        """Yield successive pages of HTML, advancing ``start`` by ``PAGE_SIZE``.

        Stub: loop up to ``max_pages``, awaiting the rate limiter between
        requests and stopping early when a page comes back empty.
        """
        raise NotImplementedError
        yield ""  # pragma: no cover - marks this as an async generator
