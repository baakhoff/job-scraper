"""Tests for the async storage layer (upsert + new-since queries).

These run against SQLite (via ``aiosqlite``) for a zero-setup offline suite; the
same code path drives Postgres (via ``asyncpg``) under Docker. ``asyncio_mode =
auto`` (see pyproject.toml) lets the async tests run without explicit markers.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest_asyncio

from src.models import JobListing, WorkplaceType
from src.storage import Storage, resolve_database_url


def _job(job_id: str, **kw: object) -> JobListing:
    base: dict[str, object] = {"job_id": job_id, "title": "Engineer", "company": "Acme"}
    base.update(kw)
    return JobListing(**base)


@pytest_asyncio.fixture
async def storage(tmp_path: Path) -> AsyncIterator[Storage]:
    """An initialized SQLite-backed Storage on a temp file, disposed on teardown."""
    store = Storage(str(tmp_path / "jobs.db"))
    await store.init_db()
    try:
        yield store
    finally:
        await store.dispose()


def test_resolve_database_url_normalizes_to_async_drivers() -> None:
    # A bare path is treated as a local SQLite file.
    assert resolve_database_url("output/jobs.db") == "sqlite+aiosqlite:///output/jobs.db"
    # Sync drivers are upgraded to their async equivalents.
    assert resolve_database_url("sqlite:///x.db") == "sqlite+aiosqlite:///x.db"
    assert (
        resolve_database_url("postgresql://ljp:ljp@db:5432/ljp")
        == "postgresql+asyncpg://ljp:ljp@db:5432/ljp"
    )
    # An already-async URL is left untouched.
    assert (
        resolve_database_url("postgresql+asyncpg://ljp@db/ljp")
        == "postgresql+asyncpg://ljp@db/ljp"
    )


async def test_save_jobs_upserts_by_job_id(storage: Storage) -> None:
    assert await storage.save_jobs([_job("1", title="Old Title")]) == 1
    # Same id again => update in place, not a new row.
    assert await storage.save_jobs([_job("1", title="New Title"), _job("2")]) == 1

    jobs = {j.job_id: j for j in await storage.get_jobs()}
    assert set(jobs) == {"1", "2"}
    assert jobs["1"].title == "New Title"


async def test_get_jobs_filters_by_keyword_and_workplace_type(storage: Storage) -> None:
    await storage.save_jobs(
        [
            _job("1", title="Python Developer", workplace_type=WorkplaceType.REMOTE),
            _job("2", title="Java Developer", workplace_type=WorkplaceType.ON_SITE),
        ]
    )

    assert [j.job_id for j in await storage.get_jobs(keyword="python")] == ["1"]
    remote = await storage.get_jobs(workplace_type=WorkplaceType.REMOTE)
    assert [j.job_id for j in remote] == ["1"]


async def test_get_new_jobs_uses_first_seen_at(storage: Storage) -> None:
    await storage.save_jobs([_job("1")])
    cutoff = datetime.now(UTC)
    await storage.save_jobs([_job("2")])

    new = await storage.get_new_jobs(cutoff)
    assert [j.job_id for j in new] == ["2"]


async def test_round_trips_enrichment_fields(storage: Storage) -> None:
    await storage.save_jobs(
        [
            _job(
                "1",
                company_url="https://www.linkedin.com/company/acme",
                description="Full description.",
                employment_type="Full-time",
                job_function="Engineering",
                industries="Software Development",
                applicant_count=200,
            )
        ]
    )
    (job,) = await storage.get_jobs()
    assert str(job.company_url) == "https://www.linkedin.com/company/acme"
    assert job.description == "Full description."
    assert job.employment_type == "Full-time"
    assert job.applicant_count == 200


async def test_update_preserves_detail_fields_on_search_only_pass(storage: Storage) -> None:
    """A later search-only upsert must not wipe earlier detail enrichment."""
    await storage.save_jobs([_job("1", description="Rich detail.", applicant_count=42)])
    # Re-seen via a plain search (no detail fields) — enrichment should survive.
    await storage.save_jobs([_job("1", title="Updated Title")])

    (job,) = await storage.get_jobs()
    assert job.title == "Updated Title"
    assert job.description == "Rich detail."
    assert job.applicant_count == 42


# --------------------------------------------------------------------------- #
# Relational model: positions, companies, people                              #
# --------------------------------------------------------------------------- #
_ACME = "https://www.linkedin.com/company/acme"
_GLOBEX = "https://www.linkedin.com/company/globex"


async def test_save_search_results_links_positions_and_companies(storage: Storage) -> None:
    jobs = [
        _job("1", company="Acme", company_url=_ACME),
        _job("2", company="Acme", company_url=_ACME),
        _job("3", company="Globex", company_url=_GLOBEX),
    ]
    result = await storage.save_search_results(jobs, keyword="Python", location="Berlin")
    assert result["new_listings"] == 3
    assert result["new_companies"] == 2

    (position,) = await storage.get_positions()
    assert position.keyword == "Python"
    assert position.company_count == 2
    assert position.listing_count == 3
    assert position.id is not None

    companies = await storage.get_companies_for_position(position.id)
    # Sorted by listing count desc: Acme (2) before Globex (1).
    assert [(c.name, c.listing_count) for c in companies] == [("Acme", 2), ("Globex", 1)]


async def test_save_search_results_dedupes_on_rerun(storage: Storage) -> None:
    jobs = [_job("1", company="Acme", company_url=_ACME)]
    first = await storage.save_search_results(jobs, keyword="Python")
    second = await storage.save_search_results(jobs, keyword="Python")
    assert first["new_listings"] == 1 and first["new_companies"] == 1
    # Re-running the same search inserts nothing new.
    assert second["new_listings"] == 0 and second["new_companies"] == 0
    assert len(await storage.get_positions()) == 1
    assert len(await storage.get_companies()) == 1


async def test_company_dedupe_prefers_slug(storage: Storage) -> None:
    # Same slug, differently-cased name => one company.
    await storage.save_search_results(
        [_job("1", company="Acme Inc", company_url=_ACME)], keyword="a"
    )
    await storage.save_search_results(
        [_job("2", company="ACME", company_url=_ACME + "?trk=x")], keyword="b"
    )
    companies = await storage.get_companies()
    assert len(companies) == 1
    assert companies[0].slug == "acme"


async def test_company_view_returns_listings_and_people(storage: Storage) -> None:
    from src.models import CompanyPerson

    await storage.save_search_results(
        [_job("1", company="Acme", company_url=_ACME)], keyword="Python"
    )
    (company,) = await storage.get_companies()
    assert company.id is not None
    new = await storage.upsert_company_people(
        company.id,
        [CompanyPerson(name="Jane Doe", headline="CEO", keyword="CEO")],
    )
    assert new == 1
    # Dedupe: same person again => no new row.
    assert await storage.upsert_company_people(company.id, [CompanyPerson(name="Jane Doe")]) == 0

    people = await storage.get_people_for_company(company.id)
    assert [p.name for p in people] == ["Jane Doe"]
    listings = await storage.get_listings_for_company(company.id)
    assert [j.job_id for j in listings] == ["1"]


async def test_backfill_links_legacy_listings_to_companies(storage: Storage) -> None:
    # Simulate legacy data: rows saved via the old flat path (no company_id).
    await storage.save_jobs([_job("1", company="Acme", company_url=_ACME)])
    # A fresh init runs the backfill.
    await storage.init_db()
    companies = await storage.get_companies()
    assert [c.name for c in companies] == ["Acme"]
    assert companies[0].listing_count == 1


async def test_update_company_patches_enrichment(storage: Storage) -> None:
    await storage.save_search_results(
        [_job("1", company="Acme", company_url=_ACME)], keyword="Python"
    )
    (company,) = await storage.get_companies()
    assert company.id is not None
    updated = await storage.update_company(
        company.id, industry="Software", website="https://acme.example"
    )
    assert updated is not None
    assert updated.industry == "Software"
    assert updated.website == "https://acme.example"


# --------------------------------------------------------------------------- #
# Position normalization                                                       #
# --------------------------------------------------------------------------- #


def test_normalize_position_keyword() -> None:
    from src.storage import normalize_position_keyword

    assert normalize_position_keyword("Senior Python Developer") == "python developer"
    assert normalize_position_keyword("Junior Python Developer") == "python developer"
    assert normalize_position_keyword("Lead Engineer") == "engineer"
    assert normalize_position_keyword("Python Developer") == "python developer"
    assert normalize_position_keyword("  Staff  Software  Engineer  ") == "software  engineer"


async def test_position_normalization_deduplicates(storage: Storage) -> None:
    listings = [_job("101", title="SWE", company="Acme")]
    await storage.save_search_results(listings, keyword="Senior Python Developer", location=None)
    await storage.save_search_results(listings, keyword="Python Developer", location=None)
    positions = await storage.get_positions()
    # Both searches should resolve to the same position (by normalized keyword).
    assert len(positions) == 1
    # keyword on Position returns display_keyword (the original first-seen search term).
    assert positions[0].keyword == "Senior Python Developer"


async def test_get_positions_for_company(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME),
            _job("2", company="Acme", company_url=_ACME),
            _job("3", company="Globex", company_url=_GLOBEX),
        ],
        keyword="Python",
        location="Berlin",
    )
    await storage.save_search_results(
        [_job("4", company="Acme", company_url=_ACME)],
        keyword="Data Scientist",
        location="Berlin",
    )
    acme = next(c for c in await storage.get_companies() if c.name == "Acme")
    assert acme.id is not None

    positions = await storage.get_positions_for_company(acme.id)
    # Acme hires for both positions; sorted by this company's listing count desc.
    assert [(p.keyword, p.listing_count) for p in positions] == [
        ("Python", 2),
        ("Data Scientist", 1),
    ]
    # company_count is the position's global distinct-company count: Python has
    # Acme + Globex (2); Data Scientist has only Acme (1).
    by_kw = {p.keyword: p for p in positions}
    assert by_kw["Python"].company_count == 2
    assert by_kw["Data Scientist"].company_count == 1


async def test_language_persisted_round_trip(storage: Storage) -> None:
    await storage.save_search_results([_job("1", language="de")], keyword="X")
    jobs = await storage.get_jobs()
    assert jobs[0].language == "de"


async def test_position_titles_group_by_normalized_title(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", title="Senior Python Developer", company="Acme", company_url=_ACME),
            _job("2", title="Python Developer", company="Globex", company_url=_GLOBEX),
            _job("3", title="Data Scientist", company="Acme", company_url=_ACME),
        ],
        keyword="mixed search",
        location=None,
    )
    titles = {t["key"]: t for t in await storage.get_position_titles()}
    # 'Senior Python Developer' + 'Python Developer' merge under 'python developer'.
    assert titles["python developer"]["listing_count"] == 2
    assert titles["python developer"]["company_count"] == 2
    assert titles["data scientist"]["listing_count"] == 1
    # Display title is one of the real titles seen in the group.
    assert titles["python developer"]["title"] in {"Senior Python Developer", "Python Developer"}

    # Drill-down by the normalized key.
    companies = await storage.get_companies_for_title("python developer")
    assert {c.name for c in companies} == {"Acme", "Globex"}
    listings = await storage.get_listings_for_title("python developer")
    assert {j.job_id for j in listings} == {"1", "2"}


async def test_position_titles_respects_language_filter(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", title="Python Developer", company="Acme", company_url=_ACME, language="en"),
            _job(
                "2", title="Python Developer", company="Globex",
                company_url=_GLOBEX, language="de",
            ),
        ],
        keyword="python",
        location=None,
    )
    de = {t["key"]: t for t in await storage.get_position_titles(language="de")}
    assert de["python developer"]["listing_count"] == 1
    assert de["python developer"]["company_count"] == 1
    de_listings = await storage.get_listings_for_title("python developer", language="de")
    assert [j.job_id for j in de_listings] == ["2"]


async def test_explore_filters_by_workplace_and_language(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME,
                 workplace_type=WorkplaceType.REMOTE, language="en"),
            _job("2", company="Globex", company_url=_GLOBEX,
                 workplace_type=WorkplaceType.ON_SITE, language="de"),
        ],
        keyword="Python",
        location="Berlin",
    )

    # Positions: a filter narrows the counts and drops non-matching positions.
    remote = await storage.get_positions(workplace_type=WorkplaceType.REMOTE)
    assert len(remote) == 1 and remote[0].listing_count == 1 and remote[0].company_count == 1
    german = await storage.get_positions(language="de")
    assert len(german) == 1 and german[0].listing_count == 1
    assert await storage.get_positions(language="fr") == []  # no match → dropped

    # Companies: only those with a matching listing are returned.
    assert [c.name for c in await storage.get_companies(workplace_type=WorkplaceType.REMOTE)] == [
        "Acme"
    ]
    assert [c.name for c in await storage.get_companies(language="de")] == ["Globex"]

    # Listings-for-position honors the same filters.
    pos = (await storage.get_positions())[0]
    assert pos.id is not None
    listings = await storage.get_listings_for_position(pos.id, language="de")
    assert [j.job_id for j in listings] == ["2"]


async def test_get_listings_needing_details(storage: Storage) -> None:
    await storage.save_jobs(
        [
            _job("1", description="Already has a description."),
            _job("2"),  # description IS NULL → needs a detail re-fetch
            _job("3"),
        ]
    )
    needing = await storage.get_listings_needing_details()
    assert {j.job_id for j in needing} == {"2", "3"}
    assert len(await storage.get_listings_needing_details(limit=1)) == 1


async def test_industry_grouping_and_filter(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME, industries="Software Development"),
            _job("2", company="Globex", company_url=_GLOBEX, industries="Software Development"),
            _job(
                "3", company="Initech",
                company_url="https://www.linkedin.com/company/initech",
                industries="Financial Services",
            ),
            _job("4", company="Acme", company_url=_ACME),  # no industry → excluded from grouping
        ],
        keyword="mixed",
        location=None,
    )
    groups = {g["key"]: g for g in await storage.get_industries()}
    assert groups["Software Development"]["listing_count"] == 2
    assert groups["Software Development"]["company_count"] == 2
    assert groups["Financial Services"]["listing_count"] == 1
    assert len(groups) == 2  # the null-industry listing is not its own group

    # Drill-down: companies in an industry.
    companies = await storage.get_companies_for_industry("Software Development")
    assert {c.name for c in companies} == {"Acme", "Globex"}

    # The industry filter narrows the other Explore queries too.
    fin = await storage.get_positions(industry="Financial Services")
    assert len(fin) == 1 and fin[0].listing_count == 1
    titles = await storage.get_position_titles(industry="Software Development")
    assert [t["listing_count"] for t in titles] == [2]  # one title group, 2 SD listings


async def test_company_industry_derived_from_listings(storage: Storage) -> None:
    # LinkedIn blocks company pages, so a company's own industry stays empty;
    # it is derived from its listings' industries (most common wins).
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME, industries="Software Development"),
            _job("2", company="Acme", company_url=_ACME, industries="Software Development"),
            _job("3", company="Acme", company_url=_ACME, industries="IT Services"),
        ],
        keyword="x",
    )
    (company,) = await storage.get_companies()
    assert company.industry == "Software Development"
    assert company.id is not None
    full = await storage.get_company(company.id)
    assert full is not None and full.industry == "Software Development"


async def test_get_companies_needing_enrichment(storage: Storage) -> None:
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME),
            _job("2", company="Globex", company_url=_GLOBEX),
            _job("3", company="NoSlug Co"),  # no company_url => no slug => not a candidate
        ],
        keyword="x",
    )
    candidates = await storage.get_companies_needing_enrichment()
    # NoSlug Co is excluded: there's no handle to build a company-page URL from.
    assert {c.name for c in candidates} == {"Acme", "Globex"}
    # limit caps the batch (name-ordered => Acme first).
    assert [c.name for c in await storage.get_companies_needing_enrichment(limit=1)] == ["Acme"]

    # Persisting any page-only field drops a company from the candidate set.
    acme = next(c for c in candidates if c.name == "Acme")
    assert acme.id is not None
    await storage.update_company(acme.id, website="https://acme.example")
    assert [c.name for c in await storage.get_companies_needing_enrichment()] == ["Globex"]


async def test_company_language_derived_from_listings(storage: Storage) -> None:
    # A company's page is usually blocked, so its language is inferred from the
    # dominant detected language of the listings it publishes.
    await storage.save_search_results(
        [
            _job("1", company="Acme", company_url=_ACME, language="ru"),
            _job("2", company="Acme", company_url=_ACME, language="ru"),
            _job("3", company="Acme", company_url=_ACME, language="en"),
        ],
        keyword="x",
    )
    (company,) = await storage.get_companies()
    assert company.language == "ru"  # most common among Acme's listings
    assert company.id is not None
    full = await storage.get_company(company.id)
    assert full is not None and full.language == "ru"
