"""Scraper tests using a mock HTTP transport (no live network)."""

from __future__ import annotations

import httpx

from src.models import SearchParams
from src.scraper import LinkedInScraper, RateLimiter

# Zero-delay limiter so tests don't actually sleep.
FAST_LIMITER = RateLimiter(0.0, 0.0, backoff_base=0.0, backoff_ceiling=0.0)

CARD = '<li><div data-entity-urn="urn:li:jobPosting:{n}"></div></li>'


def _scraper(handler: httpx.MockTransport, **kw: object) -> LinkedInScraper:
    scraper = LinkedInScraper(
        rate_limiter=FAST_LIMITER,
        user_agents=["test-agent"],
        **kw,  # type: ignore[arg-type]
    )
    scraper._client = httpx.AsyncClient(transport=handler)
    return scraper


async def test_iter_pages_stops_on_empty_page() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        calls.append(start)
        # First page returns one card, second page is empty -> stop.
        body = CARD.format(n=1) if start == 0 else ""
        return httpx.Response(200, text=body)

    scraper = _scraper(httpx.MockTransport(handler), max_pages=10)
    async with scraper:
        pages = [html async for html in scraper.iter_pages(SearchParams(keywords="x"))]

    assert len(pages) == 1
    # One card on page 0 => advance start by 1 (the real page size), not PAGE_SIZE.
    assert calls == [0, 1]  # asked for page 2, got empty, stopped


async def test_iter_pages_advances_by_actual_card_count() -> None:
    """Offset must step by the cards actually returned, not a fixed stride."""
    calls: list[int] = []
    # Page 0 serves 3 cards, page 1 serves 2, page 2 is empty.
    pages = {0: 3, 3: 2}

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        calls.append(start)
        n = pages.get(start, 0)
        body = "".join(CARD.format(n=i) for i in range(n))
        return httpx.Response(200, text=body)

    scraper = _scraper(httpx.MockTransport(handler), max_pages=10)
    async with scraper:
        out = [html async for html in scraper.iter_pages(SearchParams(keywords="x"))]

    assert len(out) == 2
    # 0 -> +3 -> 3 -> +2 -> 5 (empty, stop). No gaps, no overlap.
    assert calls == [0, 3, 5]


async def test_iter_pages_starts_from_params_start() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        start = int(request.url.params["start"])
        calls.append(start)
        return httpx.Response(200, text=CARD.format(n=1) if start == 100 else "")

    scraper = _scraper(httpx.MockTransport(handler), max_pages=10)
    async with scraper:
        [html async for html in scraper.iter_pages(SearchParams(keywords="x", start=100))]

    assert calls[0] == 100  # honours the caller's starting offset


async def test_fetch_detail_returns_text_then_empty_on_404() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/777"):
            return httpx.Response(200, text="<html>detail 777</html>")
        return httpx.Response(404, text="")

    scraper = _scraper(httpx.MockTransport(handler))
    async with scraper:
        ok = await scraper.fetch_detail("777")
        missing = await scraper.fetch_detail("000")

    assert "detail 777" in ok
    assert missing == ""


async def test_fetch_page_retries_on_429_then_succeeds() -> None:
    attempts: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text=CARD.format(n=7))

    scraper = _scraper(httpx.MockTransport(handler), max_retries=3)
    async with scraper:
        html = await scraper.fetch_page(SearchParams(keywords="x"))

    assert len(attempts) == 2  # one 429, one success
    assert "jobPosting:7" in html


async def test_fetch_page_returns_empty_on_400_end_of_results() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, text="end")

    scraper = _scraper(httpx.MockTransport(handler))
    async with scraper:
        html = await scraper.fetch_page(SearchParams(keywords="x", start=975))

    assert html == ""
