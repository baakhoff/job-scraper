"""Tests for the web JSON API against a temp-SQLite database (offline).

Endpoints that hit the network (live search, company enrich, people scrape) are
not exercised here; we seed the DB directly and assert the read/export routes
plus the graceful "people search disabled" path.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from src.models import JobListing
from src.storage import Storage


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient whose endpoints use a freshly-seeded temp SQLite DB."""
    db = (tmp_path / "web.db").as_posix()
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db}")

    async def seed() -> None:
        async with Storage() as storage:
            await storage.save_search_results(
                [
                    JobListing(
                        job_id="1", title="Python Dev", company="Acme",
                        company_url="https://www.linkedin.com/company/acme",
                    ),
                    JobListing(
                        job_id="2", title="Backend", company="Globex",
                        company_url="https://www.linkedin.com/company/globex",
                    ),
                ],
                keyword="Python",
                location="Berlin",
            )

    asyncio.run(seed())

    import web

    with TestClient(web.app) as test_client:
        yield test_client


def test_positions_endpoint(client: TestClient) -> None:
    data = client.get("/api/positions").json()
    assert data["count"] == 1
    position = data["positions"][0]
    assert position["keyword"] == "Python"
    assert position["company_count"] == 2
    assert position["listing_count"] == 2


def test_position_companies_and_company_detail(client: TestClient) -> None:
    position_id = client.get("/api/positions").json()["positions"][0]["id"]
    companies = client.get(f"/api/positions/{position_id}/companies").json()
    assert companies["count"] == 2

    company_id = companies["companies"][0]["id"]
    detail = client.get(f"/api/companies/{company_id}").json()
    assert detail["company"]["listing_count"] == 1
    assert len(detail["listings"]) == 1
    assert detail["people"] == []


def test_people_endpoint_enabled_returns_empty_on_auth_wall(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.people import NullPeopleProvider

    # Patch get_people_provider to return NullPeopleProvider so no real HTTP is made
    monkeypatch.setattr("web.get_people_provider", lambda *a, **k: NullPeopleProvider())
    company_id = client.get("/api/companies").json()["companies"][0]["id"]
    data = client.post(f"/api/companies/{company_id}/people").json()
    assert data["count"] == 0
    assert data["note"] is not None
    assert "login" in data["note"].lower() or "leader" in data["note"].lower()


def test_position_titles_endpoint(client: TestClient) -> None:
    data = client.get("/api/position-titles").json()
    # Seeded titles 'Python Dev' and 'Backend' group by normalized title.
    assert {t["key"] for t in data["titles"]} == {"python dev", "backend"}
    companies = client.get("/api/position-titles/companies", params={"title": "python dev"}).json()
    assert [c["name"] for c in companies["companies"]] == ["Acme"]
    listings = client.get("/api/position-titles/listings", params={"title": "backend"}).json()
    assert listings["count"] == 1 and listings["listings"][0]["company"] == "Globex"


def test_batch_search_runs_each_keyword(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_collect(
        params: object, target: int, *, with_details: bool = False
    ) -> list[JobListing]:
        kw = params.keywords  # type: ignore[attr-defined]
        return [
            JobListing(
                job_id=f"b-{kw}", title=kw.title(), company=f"{kw.title()} Co",
                company_url="https://www.linkedin.com/company/x",
            )
        ]

    monkeypatch.setattr("web._run_search_collecting_companies", fake_collect)
    resp = client.post(
        "/api/search/batch", json={"keywords": ["go dev", "rust dev"], "target_companies": 5}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert {r["keywords"] for r in data["results"]} == {"go dev", "rust dev"}
    assert all(r["companies"] == 1 and r["new_listings"] == 1 for r in data["results"])
    # The batch results are persisted as new searches (positions).
    positions = client.get("/api/positions").json()["positions"]
    assert any(p["keyword"] == "go dev" for p in positions)


def test_search_stream_emits_logs_then_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_events(
        params: object, *, max_results: int | None = None,
        stop: object = None, with_details: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        yield ("log", "Page 1: 1 listings, 1 companies so far…")
        yield (
            "listings",
            [
                JobListing(
                    job_id="s1", title="Go Dev", company="Go Co",
                    company_url="https://www.linkedin.com/company/x",
                )
            ],
        )

    monkeypatch.setattr("web._stream_search_events", fake_events)
    resp = client.post("/api/search/stream", json={"keywords": "go dev", "max_results": 25})
    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    assert "log" in {e["type"] for e in events}            # progress streamed
    result = events[-1]
    assert result["type"] == "result"
    assert result["count"] == 1 and result["jobs"][0]["company"] == "Go Co"
    # Persisted as a searchable position.
    positions = client.get("/api/positions").json()["positions"]
    assert any(p["keyword"] == "go dev" for p in positions)


def test_search_stream_surfaces_midstream_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(
        params: object, *, max_results: int | None = None,
        stop: object = None, with_details: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        yield ("log", "Page 1…")
        raise RuntimeError("scrape blew up")

    monkeypatch.setattr("web._stream_search_events", boom)
    resp = client.post("/api/search/stream", json={"keywords": "go dev"})
    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    # A failure mid-stream is reported as an explicit error event, not a silent cut-off.
    assert events[-1]["type"] == "error"
    assert "blew up" in events[-1]["message"]


def test_batch_stream_emits_per_keyword_result(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_events(
        params: object, *, max_results: int | None = None,
        stop: object = None, with_details: bool = False,
    ) -> AsyncIterator[tuple[str, object]]:
        kw = params.keywords  # type: ignore[attr-defined]
        yield ("log", "Page 1…")
        yield (
            "listings",
            [
                JobListing(
                    job_id=f"b-{kw}", title=kw.title(), company=f"{kw.title()} Co",
                    company_url="https://www.linkedin.com/company/x",
                )
            ],
        )

    monkeypatch.setattr("web._stream_search_events", fake_events)
    resp = client.post(
        "/api/search/batch/stream",
        json={"keywords": ["go dev", "rust dev"], "target_companies": 5},
    )
    assert resp.status_code == 200
    events = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    result = events[-1]
    assert result["type"] == "result" and result["count"] == 2
    assert {r["keywords"] for r in result["results"]} == {"go dev", "rust dev"}


def test_export_listings_csv(client: TestClient) -> None:
    res = client.get("/api/export/listings.csv")
    assert res.status_code == 200
    assert res.headers["content-disposition"] == 'attachment; filename="listings.csv"'
    lines = res.text.splitlines()
    assert lines[0].startswith("job_id,title,company")
    assert len(lines) == 3  # header + 2 rows


def test_export_companies_json(client: TestClient) -> None:
    res = client.get("/api/export/companies.json")
    assert res.status_code == 200
    names = {row["name"] for row in res.json()}
    assert names == {"Acme", "Globex"}


def test_unknown_export_entity_404(client: TestClient) -> None:
    assert client.get("/api/export/widgets.csv").status_code == 404
