"""Async HTTP scraper for the public LinkedIn jobs endpoint.

Targets the *guest* job-search API (no login required):

    https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search

which returns a chunk of raw HTML job cards. Pagination is offset-based via
the ``start`` parameter (page size 25). Rate limiting is essential — LinkedIn
will return 429 / soft-block aggressively under load.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator, Sequence

import httpx
import structlog

from .models import SearchParams

log = structlog.get_logger(__name__)

GUEST_SEARCH_URL = "https://www.linkedin.com/jobs-guest/jobs/api/seeMoreJobPostings/search"
PAGE_SIZE = 25


class RateLimiter:
    """Async delay gate between requests with exponential 429 backoff.

    ``wait()`` sleeps a random duration in ``[delay_min, delay_max]`` so the
    request cadence is jittered rather than constant. ``backoff()`` sleeps an
    exponentially growing duration (capped) and is called after a 429.
    """

    def __init__(
        self,
        delay_min: float,
        delay_max: float,
        *,
        backoff_base: float = 5.0,
        backoff_ceiling: float = 120.0,
    ) -> None:
        if delay_max < delay_min:
            delay_min, delay_max = delay_max, delay_min
        self.delay_min = delay_min
        self.delay_max = delay_max
        self.backoff_base = backoff_base
        self.backoff_ceiling = backoff_ceiling

    async def wait(self) -> None:
        """Sleep a random delay in ``[delay_min, delay_max]`` before the next request."""
        await asyncio.sleep(random.uniform(self.delay_min, self.delay_max))

    async def backoff(self, attempt: int) -> None:
        """Sleep an exponentially increasing delay after a soft-block (429)."""
        delay = min(self.backoff_base * (2**attempt), self.backoff_ceiling)
        # Add jitter so retries from concurrent clients don't sync up.
        delay += random.uniform(0, self.backoff_base)
        log.warning("rate_limited_backoff", attempt=attempt, sleep_seconds=round(delay, 1))
        await asyncio.sleep(delay)


class LinkedInScraper:
    """Fetches raw job-card HTML pages from the guest endpoint.

    Owns an ``httpx.AsyncClient`` and a ``RateLimiter``. Yields raw HTML
    strings; turning HTML into dicts is the parser's job. Rotates through a
    pool of User-Agents per request to look less like a single bot.
    """

    def __init__(
        self,
        *,
        rate_limiter: RateLimiter,
        user_agents: Sequence[str],
        timeout: float = 15.0,
        max_pages: int = 40,
        max_results: int | None = None,
        max_retries: int = 4,
    ) -> None:
        if not user_agents:
            raise ValueError("at least one user agent is required")
        self.rate_limiter = rate_limiter
        self.user_agents = list(user_agents)
        self.max_pages = max_pages
        self.max_results = max_results
        self.max_retries = max_retries
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def __aenter__(self) -> LinkedInScraper:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    def _headers(self) -> dict[str, str]:
        """Build request headers with a randomly chosen User-Agent."""
        return {
            "User-Agent": random.choice(self.user_agents),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": "https://www.linkedin.com/jobs",
        }

    async def fetch_page(self, params: SearchParams) -> str:
        """Fetch a single page of job-card HTML for the given params.

        Retries on 429 (with exponential backoff) and on transient network
        errors, up to ``max_retries``. Returns ``response.text`` (an HTML
        fragment of ``<li>`` cards). Returns ``""`` only on a non-retryable
        empty/blocked terminal state.
        """
        url = httpx.URL(GUEST_SEARCH_URL, params=params.to_query())
        for attempt in range(self.max_retries):
            try:
                response = await self._client.get(url, headers=self._headers())
            except httpx.TransportError as exc:
                log.warning("request_failed", error=str(exc), attempt=attempt)
                await self.rate_limiter.backoff(attempt)
                continue

            if response.status_code == 429:
                await self.rate_limiter.backoff(attempt)
                continue
            if response.status_code == 400:
                # LinkedIn returns 400 once you page past the available results.
                log.info("end_of_results", start=params.start, status=400)
                return ""
            response.raise_for_status()
            return response.text

        log.error("max_retries_exceeded", start=params.start)
        return ""

    async def iter_pages(self, params: SearchParams) -> AsyncIterator[str]:
        """Yield successive pages of HTML, advancing ``start`` by ``PAGE_SIZE``.

        Loops up to ``max_pages`` (and, if set, until ``max_results`` cards have
        been seen), awaiting the rate limiter between requests and stopping
        early when a page comes back empty.
        """
        seen = 0
        for page in range(self.max_pages):
            start = page * PAGE_SIZE
            if self.max_results is not None and seen >= self.max_results:
                break

            page_params = params.model_copy(update={"start": start})
            if page > 0:
                await self.rate_limiter.wait()

            html = await self.fetch_page(page_params)
            stripped = html.strip()
            if not stripped:
                log.info("empty_page_stop", page=page, start=start)
                break

            # Cheap card count for the max_results gate (one <li> per card).
            seen += stripped.count("<li")
            log.info("page_fetched", page=page, start=start, approx_cards_total=seen)
            yield html
