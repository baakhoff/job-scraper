"""Persistence layer: store parsed listings via async SQLAlchemy.

Uses the SQLAlchemy 2.0 *async* API. Two backends are supported through the
same code path:

* **PostgreSQL** (``postgresql+asyncpg://``) — the Docker / production target.
* **SQLite** (``sqlite+aiosqlite://``) — the zero-setup local-dev fallback.

The backend is chosen by URL (see :func:`resolve_database_url`): the
``DATABASE_URL`` environment variable wins, then ``config.database_url``. A bare
filesystem path (as the tests and the old ``LJP_DB_PATH`` pass) is treated as a
SQLite file for backwards compatibility.

The schema is intentionally close to ``models.JobListing``; ``job_id`` is the
natural primary key so re-runs upsert rather than duplicate. A ``first_seen_at``
column records when a row was *first* inserted, which powers
:meth:`Storage.get_new_jobs` (used by the Telegram notification hook to surface
only jobs added since the last run).
"""

from __future__ import annotations

import os
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import (
    Connection,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
    select,
    text,
)
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import (
    Company,
    CompanyPerson,
    JobListing,
    Position,
    WorkplaceType,
    company_slug,
    normalize_company_name,
)

log = structlog.get_logger(__name__)

_SENIORITY_RE = re.compile(
    r"^(senior|sr\.?|junior|jr\.?|lead|staff|principal|associate|"
    r"mid[\s\-]?level|entry[\s\-]?level|head\s+of|vp\s+of|director\s+of|chief)\s+",
    re.IGNORECASE,
)


def normalize_position_keyword(kw: str) -> str:
    """Strip common seniority/level prefixes for position dedup.

    "Senior Python Developer" and "Python Developer" normalize to the same
    keyword and are stored under a single position row.
    """
    text = kw.strip().lower()
    prev = None
    while prev != text:
        prev = text
        text = _SENIORITY_RE.sub("", text).strip()
    return text


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


class JobRecord(Base):
    """ORM row mirroring a :class:`~src.models.JobListing`."""

    __tablename__ = "job_listings"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    company_url: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    workplace_type: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    description_snippet: Mapped[str | None] = mapped_column(String, nullable=True)
    salary: Mapped[str | None] = mapped_column(String, nullable=True)
    seniority: Mapped[str | None] = mapped_column(String, nullable=True)
    employment_type: Mapped[str | None] = mapped_column(String, nullable=True)
    job_function: Mapped[str | None] = mapped_column(String, nullable=True)
    industries: Mapped[str | None] = mapped_column(String, nullable=True)
    applicant_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # Relational links, added in the Positions/Companies model. Nullable so
    # legacy rows (and back-compat ``save_jobs``) remain valid; new searches
    # populate them via ``save_search_results``.
    position_id: Mapped[int | None] = mapped_column(
        ForeignKey("positions.id"), nullable=True, index=True
    )
    company_id: Mapped[int | None] = mapped_column(
        ForeignKey("companies.id"), nullable=True, index=True
    )

    def to_listing(self) -> JobListing:
        """Reconstruct a domain :class:`JobListing` from this row."""
        return JobListing(
            job_id=self.job_id,
            title=self.title,
            company=self.company,
            company_url=self.company_url,
            location=self.location,
            workplace_type=WorkplaceType(self.workplace_type) if self.workplace_type else None,
            url=self.url,
            posted_at=self.posted_at,
            description=self.description,
            description_snippet=self.description_snippet,
            salary=self.salary,
            seniority=self.seniority,
            employment_type=self.employment_type,
            job_function=self.job_function,
            industries=self.industries,
            applicant_count=self.applicant_count,
        )


# Alias so the table name reads naturally where the task refers to it.
JobListingORM = JobRecord


class PositionRecord(Base):
    """A searched role; groups the listings (and thus companies) found for it."""

    __tablename__ = "positions"
    __table_args__ = (UniqueConstraint("keyword", "location", name="uq_position_keyword_loc"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    # Normalized (lowercased) form used for de-duplication / lookup.
    keyword: Mapped[str] = mapped_column(String, index=True)
    # Original-case keyword for display.
    display_keyword: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CompanyRecord(Base):
    """A company aggregated from listings; enrichment columns fill in on demand."""

    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String)
    # Lowercased name, the fallback de-dupe key when no slug is available.
    normalized_name: Mapped[str] = mapped_column(String, index=True)
    # The '/company/{slug}' handle — the preferred de-dupe key when present.
    slug: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    company_url: Mapped[str | None] = mapped_column(String, nullable=True)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    industry: Mapped[str | None] = mapped_column(String, nullable=True)
    company_size: Mapped[str | None] = mapped_column(String, nullable=True)
    website: Mapped[str | None] = mapped_column(String, nullable=True)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_company(self, *, listing_count: int = 0) -> Company:
        """Reconstruct a domain :class:`Company` from this row."""
        return Company(
            id=self.id,
            name=self.name,
            company_url=self.company_url,
            slug=self.slug,
            location=self.location,
            industry=self.industry,
            company_size=self.company_size,
            website=self.website,
            description=self.description,
            listing_count=listing_count,
        )


class CompanyPersonRecord(Base):
    """A person linked to a company (e.g. CEO / founder)."""

    __tablename__ = "company_people"
    __table_args__ = (
        UniqueConstraint("company_id", "name", name="uq_person_company_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    name: Mapped[str] = mapped_column(String)
    headline: Mapped[str | None] = mapped_column(String, nullable=True)
    profile_url: Mapped[str | None] = mapped_column(String, nullable=True)
    keyword: Mapped[str | None] = mapped_column(String, nullable=True)
    source: Mapped[str | None] = mapped_column(String, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    def to_person(self) -> CompanyPerson:
        """Reconstruct a domain :class:`CompanyPerson` from this row."""
        return CompanyPerson(
            id=self.id,
            name=self.name,
            headline=self.headline,
            profile_url=self.profile_url,
            keyword=self.keyword,
            source=self.source,
        )


def resolve_database_url(source: str | None = None) -> str:
    """Resolve the SQLAlchemy URL to use, normalizing to an async driver.

    Precedence: explicit ``source`` argument, then the ``DATABASE_URL``
    environment variable (set by Docker Compose), then ``config.database_url``.

    Values are normalized so callers can pass either a full URL or — for
    backwards compatibility with the old ``LJP_DB_PATH`` and the test suite — a
    bare SQLite filesystem path. Sync drivers are upgraded to their async
    equivalents (``postgresql://`` → ``postgresql+asyncpg://``, ``sqlite://`` →
    ``sqlite+aiosqlite://``) so the async engine can use them.
    """
    raw = source or os.getenv("DATABASE_URL")
    if not raw:
        # Imported lazily to avoid a hard import-time dependency on the config
        # module (keeps storage usable in isolation / tests).
        from config import config

        raw = config.database_url

    if "://" not in raw:
        # A bare path like ``output/jobs.db`` — treat as a local SQLite file.
        return f"sqlite+aiosqlite:///{raw}"

    scheme, rest = raw.split("://", 1)
    if scheme == "postgresql" or scheme == "postgres":
        return f"postgresql+asyncpg://{rest}"
    if scheme == "sqlite":
        return f"sqlite+aiosqlite://{rest}"
    return raw


class Storage:
    """Async wrapper around a SQLAlchemy engine for reading/writing listings.

    Usable as an async context manager, which initializes the schema on entry
    and disposes the engine on exit::

        async with Storage() as storage:
            await storage.save_jobs(listings)
    """

    def __init__(self, url: str | None = None) -> None:
        self.url = resolve_database_url(url)
        if self.url.startswith("sqlite") and ":memory:" not in self.url:
            self._ensure_sqlite_parent_dir()
        self.engine: AsyncEngine = create_async_engine(self.url)

    def _ensure_sqlite_parent_dir(self) -> None:
        """Create the parent directory for a SQLite file before the engine opens it."""
        # ``sqlite+aiosqlite:///output/jobs.db`` -> ``output/jobs.db``.
        path = self.url.split("///", 1)[-1]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    async def __aenter__(self) -> Storage:
        await self.init_db()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.dispose()

    async def dispose(self) -> None:
        """Release the engine's connection pool."""
        await self.engine.dispose()

    async def init_db(self) -> None:
        """Create tables if absent, then patch any columns missing on old DBs.

        ``create_all`` only creates whole tables — it will not add a column to a
        pre-existing ``job_listings`` table. So on SQLite (the long-lived local
        dev file) we additionally diff the live schema against the model and
        ``ALTER TABLE ADD COLUMN`` the gaps, letting an older database pick up
        new fields without a manual migration or a data wipe. On Postgres we
        rely on ``create_all`` for first-run init (and Alembic for anything
        beyond that, should the project grow it).
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            if self.engine.dialect.name == "sqlite":
                await conn.run_sync(_add_missing_sqlite_columns)
            elif self.engine.dialect.name == "postgresql":
                await conn.run_sync(_add_missing_pg_columns)
        await self._backfill_companies()

    async def _backfill_companies(self) -> None:
        """Link legacy listings (``company_id IS NULL``) to ``companies`` rows.

        Older databases predate the relational schema: their ``job_listings``
        rows have no ``company_id``. Derive a company from each such row's stored
        ``company`` / ``company_url`` and link it, so the Companies/Explore views
        include historical data. Positions can't be backfilled (the old schema
        never stored the search term), so ``position_id`` is left NULL.
        """
        now = datetime.now(UTC)
        async with AsyncSession(self.engine) as session:
            stmt = select(JobRecord).where(JobRecord.company_id.is_(None))
            rows: Sequence[JobRecord] = (await session.scalars(stmt)).all()
            if not rows:
                return
            for row in rows:
                company, _ = await self._get_or_create_company(
                    session, name=row.company, company_url=row.company_url, now=now
                )
                row.company_id = company.id
            await session.commit()
            log.info("company_backfill", linked=len(rows))

    async def save_jobs(self, listings: Iterable[JobListing]) -> int:
        """Insert or update listings keyed by ``job_id``; return rows newly inserted.

        Existing rows are updated in place (their ``first_seen_at`` is
        preserved); brand-new rows get ``first_seen_at`` stamped to now. The
        return value is the count of *new* listings, which is what callers
        typically report ("found N new jobs").
        """
        now = datetime.now(UTC)
        new_count = 0
        async with AsyncSession(self.engine) as session:
            for listing in listings:
                existing = await session.get(JobRecord, listing.job_id)
                if existing is None:
                    session.add(_to_record(listing, first_seen_at=now))
                    new_count += 1
                else:
                    _update_record(existing, listing)
            await session.commit()
        log.info("jobs_saved", new=new_count)
        return new_count

    # Back-compat alias used by the original scaffold wiring.
    async def upsert_many(self, listings: Iterable[JobListing]) -> int:
        """Alias for :meth:`save_jobs`."""
        return await self.save_jobs(listings)

    async def get_jobs(
        self,
        *,
        keyword: str | None = None,
        company: str | None = None,
        workplace_type: WorkplaceType | None = None,
        limit: int | None = None,
    ) -> list[JobListing]:
        """Read stored listings back as domain models, newest-first.

        Optional filters narrow by a substring keyword (title/company),
        company name, or workplace type.
        """
        stmt = select(JobRecord).order_by(JobRecord.posted_at.desc().nullslast())
        if keyword:
            stmt = stmt.where(
                JobRecord.title.icontains(keyword) | JobRecord.company.icontains(keyword)
            )
        if company:
            stmt = stmt.where(JobRecord.company.icontains(company))
        if workplace_type is not None:
            stmt = stmt.where(JobRecord.workplace_type == workplace_type.value)
        if limit is not None:
            stmt = stmt.limit(limit)

        async with AsyncSession(self.engine) as session:
            rows: Sequence[JobRecord] = (await session.scalars(stmt)).all()
            return [row.to_listing() for row in rows]

    async def get_new_jobs(self, since: datetime) -> list[JobListing]:
        """Return listings first seen strictly after ``since`` (for notifications)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        stmt = (
            select(JobRecord)
            .where(JobRecord.first_seen_at > since)
            .order_by(JobRecord.first_seen_at.desc())
        )
        async with AsyncSession(self.engine) as session:
            rows: Sequence[JobRecord] = (await session.scalars(stmt)).all()
            return [row.to_listing() for row in rows]

    async def all_listings(self) -> list[JobListing]:
        """Read every stored listing back as ``JobListing`` models."""
        return await self.get_jobs()

    # ------------------------------------------------------------------ #
    # Relational model: positions, companies, people                     #
    # ------------------------------------------------------------------ #

    def _session(self) -> AsyncSession:
        """Session that keeps attributes usable after commit (no async lazy-load)."""
        return AsyncSession(self.engine, expire_on_commit=False)

    async def save_search_results(
        self,
        listings: Iterable[JobListing],
        *,
        keyword: str,
        location: str | None = None,
    ) -> dict[str, int]:
        """Persist a search: upsert the position, dedupe companies, link listings.

        De-duplication is *insert-if-absent*: a listing already present (by
        ``job_id``) is updated in place; a company already present (by slug, else
        normalized name) is reused; the position is reused by
        ``(keyword, location)``. Returns counts ``{new_listings, new_companies,
        total_listings}``.
        """
        now = datetime.now(UTC)
        rows = list(listings)
        new_listings = 0
        new_company_ids: set[int] = set()
        async with self._session() as session:
            position = await self._get_or_create_position(
                session, keyword=keyword, location=location, now=now
            )
            for listing in rows:
                company, created = await self._get_or_create_company(
                    session,
                    name=listing.company,
                    company_url=str(listing.company_url) if listing.company_url else None,
                    now=now,
                )
                if created and company.id is not None:
                    new_company_ids.add(company.id)
                existing = await session.get(JobRecord, listing.job_id)
                if existing is None:
                    record = _to_record(listing, first_seen_at=now)
                    record.position_id = position.id
                    record.company_id = company.id
                    session.add(record)
                    new_listings += 1
                else:
                    _update_record(existing, listing)
                    existing.position_id = position.id
                    existing.company_id = company.id
            await session.commit()
        result = {
            "position_id": position.id,
            "new_listings": new_listings,
            "new_companies": len(new_company_ids),
            "total_listings": len(rows),
        }
        log.info("search_results_saved", **result)
        return result

    async def _get_or_create_position(
        self, session: AsyncSession, *, keyword: str, location: str | None, now: datetime
    ) -> PositionRecord:
        """Find a position by (normalized keyword, location) or create it."""
        norm = normalize_position_keyword(keyword)
        loc = location or None
        loc_cond = (
            PositionRecord.location.is_(None) if loc is None else PositionRecord.location == loc
        )
        stmt = select(PositionRecord).where(PositionRecord.keyword == norm, loc_cond).limit(1)
        existing = (await session.scalars(stmt)).first()
        if existing is not None:
            return existing
        record = PositionRecord(
            keyword=norm, display_keyword=keyword, location=loc, created_at=now
        )
        session.add(record)
        await session.flush()  # assign primary key for linking
        return record

    async def _get_or_create_company(
        self, session: AsyncSession, *, name: str, company_url: str | None, now: datetime
    ) -> tuple[CompanyRecord, bool]:
        """Find a company by slug (else normalized name) or create it.

        Returns ``(record, created)``. Opportunistically backfills a missing
        url/slug on an existing row.
        """
        slug = company_slug(company_url)
        norm = normalize_company_name(name) or name.strip().lower()
        if slug:
            stmt = select(CompanyRecord).where(CompanyRecord.slug == slug)
        else:
            stmt = select(CompanyRecord).where(
                CompanyRecord.normalized_name == norm, CompanyRecord.slug.is_(None)
            )
        existing = (await session.scalars(stmt.limit(1))).first()
        if existing is not None:
            if company_url and not existing.company_url:
                existing.company_url = company_url
            if slug and not existing.slug:
                existing.slug = slug
            return existing, False
        record = CompanyRecord(
            name=name,
            normalized_name=norm,
            slug=slug,
            company_url=company_url,
            first_seen_at=now,
        )
        session.add(record)
        await session.flush()  # assign primary key for linking
        return record, True

    async def get_positions(self) -> list[Position]:
        """All searched positions with company + listing counts, newest-first."""
        async with self._session() as session:
            records = (
                await session.scalars(
                    select(PositionRecord).order_by(PositionRecord.created_at.desc())
                )
            ).all()
            positions: list[Position] = []
            for rec in records:
                listing_count = (
                    await session.scalar(
                        select(func.count())
                        .select_from(JobRecord)
                        .where(JobRecord.position_id == rec.id)
                    )
                ) or 0
                company_count = (
                    await session.scalar(
                        select(func.count(func.distinct(JobRecord.company_id))).where(
                            JobRecord.position_id == rec.id
                        )
                    )
                ) or 0
                positions.append(
                    Position(
                        id=rec.id,
                        keyword=rec.display_keyword,
                        location=rec.location,
                        company_count=company_count,
                        listing_count=listing_count,
                    )
                )
            return positions

    async def get_companies_for_position(self, position_id: int) -> list[Company]:
        """Companies hiring for a position (derived from listings), by listing count."""
        async with self._session() as session:
            stmt = (
                select(JobRecord.company_id, func.count().label("cnt"))
                .where(JobRecord.position_id == position_id, JobRecord.company_id.is_not(None))
                .group_by(JobRecord.company_id)
            )
            pairs = (await session.execute(stmt)).all()
            companies: list[Company] = []
            for company_id, count in pairs:
                rec = await session.get(CompanyRecord, company_id)
                if rec is not None:
                    companies.append(rec.to_company(listing_count=count))
            companies.sort(key=lambda c: c.listing_count, reverse=True)
            return companies

    async def get_companies(
        self, *, keyword: str | None = None, limit: int | None = None
    ) -> list[Company]:
        """All stored companies (optional name filter), with listing counts."""
        async with self._session() as session:
            counts = {
                cid: cnt
                for cid, cnt in (
                    await session.execute(
                        select(JobRecord.company_id, func.count())
                        .where(JobRecord.company_id.is_not(None))
                        .group_by(JobRecord.company_id)
                    )
                ).all()
            }
            stmt = select(CompanyRecord).order_by(CompanyRecord.name)
            if keyword:
                stmt = stmt.where(CompanyRecord.name.icontains(keyword))
            if limit is not None:
                stmt = stmt.limit(limit)
            records = (await session.scalars(stmt)).all()
            return [rec.to_company(listing_count=counts.get(rec.id, 0)) for rec in records]

    async def get_company(self, company_id: int) -> Company | None:
        """A single company with its listing count, or ``None``."""
        async with self._session() as session:
            rec = await session.get(CompanyRecord, company_id)
            if rec is None:
                return None
            count = (
                await session.scalar(
                    select(func.count())
                    .select_from(JobRecord)
                    .where(JobRecord.company_id == company_id)
                )
            ) or 0
            return rec.to_company(listing_count=count)

    async def update_company(self, company_id: int, **fields: object) -> Company | None:
        """Patch non-null enrichment fields on a company; return the updated row."""
        async with self._session() as session:
            rec = await session.get(CompanyRecord, company_id)
            if rec is None:
                return None
            for key, value in fields.items():
                if value is not None and hasattr(rec, key):
                    setattr(rec, key, value)
            await session.commit()
            count = (
                await session.scalar(
                    select(func.count())
                    .select_from(JobRecord)
                    .where(JobRecord.company_id == company_id)
                )
            ) or 0
            return rec.to_company(listing_count=count)

    async def get_listings_for_company(self, company_id: int) -> list[JobListing]:
        """All listings stored for a company, newest-first."""
        async with self._session() as session:
            records = (
                await session.scalars(
                    select(JobRecord)
                    .where(JobRecord.company_id == company_id)
                    .order_by(JobRecord.posted_at.desc().nullslast())
                )
            ).all()
            return [rec.to_listing() for rec in records]

    async def get_listing(self, job_id: str) -> JobListing | None:
        """A single listing by ``job_id``, or ``None``."""
        async with self._session() as session:
            rec = await session.get(JobRecord, job_id)
            return rec.to_listing() if rec is not None else None

    async def update_listing(self, job_id: str, **fields: object) -> JobListing | None:
        """Patch non-null enrichment fields on a listing; return the updated row."""
        async with self._session() as session:
            rec = await session.get(JobRecord, job_id)
            if rec is None:
                return None
            for key, value in fields.items():
                if value is not None and hasattr(rec, key):
                    setattr(rec, key, value)
            await session.commit()
            return rec.to_listing()

    async def upsert_company_people(
        self, company_id: int, people: Iterable[CompanyPerson]
    ) -> int:
        """Insert people for a company, deduped by profile_url else name. Returns new count."""
        now = datetime.now(UTC)
        new = 0
        async with self._session() as session:
            for person in people:
                stmt = select(CompanyPersonRecord).where(
                    CompanyPersonRecord.company_id == company_id
                )
                if person.profile_url:
                    stmt = stmt.where(
                        CompanyPersonRecord.profile_url == str(person.profile_url)
                    )
                else:
                    stmt = stmt.where(CompanyPersonRecord.name == person.name)
                existing = (await session.scalars(stmt.limit(1))).first()
                if existing is None:
                    session.add(
                        CompanyPersonRecord(
                            company_id=company_id,
                            name=person.name,
                            headline=person.headline,
                            profile_url=str(person.profile_url) if person.profile_url else None,
                            keyword=person.keyword,
                            source=person.source,
                            first_seen_at=now,
                        )
                    )
                    new += 1
                else:
                    if person.headline and not existing.headline:
                        existing.headline = person.headline
                    if person.keyword and not existing.keyword:
                        existing.keyword = person.keyword
            await session.commit()
        log.info("company_people_saved", company_id=company_id, new=new)
        return new

    async def get_people_for_company(self, company_id: int) -> list[CompanyPerson]:
        """All people stored for a company, oldest-first."""
        async with self._session() as session:
            records = (
                await session.scalars(
                    select(CompanyPersonRecord)
                    .where(CompanyPersonRecord.company_id == company_id)
                    .order_by(CompanyPersonRecord.first_seen_at)
                )
            ).all()
            return [rec.to_person() for rec in records]

    async def all_people(self) -> list[tuple[int, CompanyPerson]]:
        """Every stored person paired with its company id (for export)."""
        async with self._session() as session:
            records = (
                await session.scalars(
                    select(CompanyPersonRecord).order_by(CompanyPersonRecord.company_id)
                )
            ).all()
            return [(rec.company_id, rec.to_person()) for rec in records]


def _add_missing_pg_columns(conn: Connection) -> None:
    """Add new ``job_listings`` FK columns to a pre-existing Postgres table.

    Postgres ``create_all`` won't alter an existing table, so add the relational
    columns idempotently (``ADD COLUMN IF NOT EXISTS``). Fresh installs get the
    full columns — including the FK constraints — from ``create_all`` directly.
    """
    table_name = JobRecord.__tablename__
    for column in (JobRecord.__table__.c.position_id, JobRecord.__table__.c.company_id):
        col_type = column.type.compile(conn.dialect)
        conn.execute(
            text(
                f'ALTER TABLE "{table_name}" '
                f'ADD COLUMN IF NOT EXISTS "{column.name}" {col_type}'
            )
        )


def _add_missing_sqlite_columns(conn: Connection) -> None:
    """Lightweight forward-only migration: add model columns absent from SQLite.

    Runs inside ``conn.run_sync`` (a plain sync :class:`Connection`) because
    PRAGMA / ALTER introspection has no clean async-streaming equivalent.
    """
    table_name = JobRecord.__tablename__
    existing = {
        row[1]  # PRAGMA table_info: (cid, name, type, ...)
        for row in conn.execute(text(f'PRAGMA table_info("{table_name}")'))
    }
    for column in JobRecord.__table__.columns:
        if column.name in existing:
            continue
        col_type = column.type.compile(conn.dialect)
        conn.execute(text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}'))
        log.info("schema_column_added", column=column.name)


def _to_record(listing: JobListing, *, first_seen_at: datetime) -> JobRecord:
    """Map a domain listing onto a new ORM row."""
    return JobRecord(
        job_id=listing.job_id,
        title=listing.title,
        company=listing.company,
        company_url=str(listing.company_url) if listing.company_url else None,
        location=listing.location,
        workplace_type=listing.workplace_type.value if listing.workplace_type else None,
        url=str(listing.url) if listing.url else None,
        posted_at=listing.posted_at,
        description=listing.description,
        description_snippet=listing.description_snippet,
        salary=listing.salary,
        seniority=listing.seniority,
        employment_type=listing.employment_type,
        job_function=listing.job_function,
        industries=listing.industries,
        applicant_count=listing.applicant_count,
        first_seen_at=first_seen_at,
    )


def _update_record(record: JobRecord, listing: JobListing) -> None:
    """Update a mutable ORM row in place from a listing (keeps ``first_seen_at``).

    Detail-only fields are only overwritten when the incoming listing actually
    carries them, so a later cheap search-only pass doesn't wipe enrichment
    (description, applicant count, …) captured by an earlier ``--details`` run.
    """
    record.title = listing.title
    record.company = listing.company
    record.location = listing.location
    record.workplace_type = listing.workplace_type.value if listing.workplace_type else None
    record.url = str(listing.url) if listing.url else None
    record.posted_at = listing.posted_at
    if listing.company_url:
        record.company_url = str(listing.company_url)
    if listing.description_snippet:
        record.description_snippet = listing.description_snippet
    for field in (
        "description",
        "salary",
        "seniority",
        "employment_type",
        "job_function",
        "industries",
        "applicant_count",
    ):
        value = getattr(listing, field)
        if value is not None:
            setattr(record, field, value)
