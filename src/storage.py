"""Persistence layer: store parsed listings in SQLite via SQLAlchemy.

Uses the SQLAlchemy 2.0 declarative API. The schema is intentionally close
to ``models.JobListing``; ``job_id`` is the natural primary key so re-runs
upsert rather than duplicate.

A ``first_seen_at`` column records when a row was *first* inserted, which is
what powers :meth:`Storage.get_new_jobs` (used by the Telegram notification
hook to surface only jobs added since the last run).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

import structlog
from sqlalchemy import DateTime, Integer, String, create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from .models import JobListing, WorkplaceType

log = structlog.get_logger(__name__)


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


class Storage:
    """Thin wrapper around a SQLAlchemy engine for reading/writing listings."""

    def __init__(self, db_path: str) -> None:
        # Ensure the parent directory (e.g. ``output/``) exists before SQLite
        # tries to create the file.
        parent = os.path.dirname(db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.engine: Engine = create_engine(f"sqlite:///{db_path}")
        self.init_db()

    def init_db(self) -> None:
        """Create tables if absent, then add any columns missing on old DBs.

        ``create_all`` only creates whole tables — it will not add a column to a
        pre-existing ``job_listings`` table. So after creating, we diff the live
        schema against the model and ``ALTER TABLE ADD COLUMN`` the gaps. This
        lets a database from an earlier schema pick up new fields (company_url,
        employment_type, …) without a manual migration or a data wipe.
        """
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()

    def _add_missing_columns(self) -> None:
        """Lightweight forward-only migration: add model columns absent from SQLite."""
        table_name = JobRecord.__tablename__
        with self.engine.begin() as conn:
            existing = {
                row[1]  # PRAGMA table_info: (cid, name, type, ...)
                for row in conn.execute(text(f'PRAGMA table_info("{table_name}")'))
            }
            for column in JobRecord.__table__.columns:
                if column.name in existing:
                    continue
                col_type = column.type.compile(self.engine.dialect)
                conn.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN "{column.name}" {col_type}')
                )
                log.info("schema_column_added", column=column.name)

    def save_jobs(self, listings: Iterable[JobListing]) -> int:
        """Insert or update listings keyed by ``job_id``; return rows newly inserted.

        Existing rows are updated in place (their ``first_seen_at`` is
        preserved); brand-new rows get ``first_seen_at`` stamped to now. The
        return value is the count of *new* listings, which is what callers
        typically report ("found N new jobs").
        """
        now = datetime.now(UTC)
        new_count = 0
        with Session(self.engine) as session:
            for listing in listings:
                existing = session.get(JobRecord, listing.job_id)
                if existing is None:
                    session.add(_to_record(listing, first_seen_at=now))
                    new_count += 1
                else:
                    _update_record(existing, listing)
            session.commit()
        log.info("jobs_saved", new=new_count)
        return new_count

    # Back-compat alias used by the original scaffold wiring.
    def upsert_many(self, listings: Iterable[JobListing]) -> int:
        """Alias for :meth:`save_jobs`."""
        return self.save_jobs(listings)

    def get_jobs(
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

        with Session(self.engine) as session:
            rows: Sequence[JobRecord] = session.scalars(stmt).all()
            return [row.to_listing() for row in rows]

    def get_new_jobs(self, since: datetime) -> list[JobListing]:
        """Return listings first seen strictly after ``since`` (for notifications)."""
        if since.tzinfo is None:
            since = since.replace(tzinfo=UTC)
        stmt = (
            select(JobRecord)
            .where(JobRecord.first_seen_at > since)
            .order_by(JobRecord.first_seen_at.desc())
        )
        with Session(self.engine) as session:
            rows: Sequence[JobRecord] = session.scalars(stmt).all()
            return [row.to_listing() for row in rows]

    def all_listings(self) -> list[JobListing]:
        """Read every stored listing back as ``JobListing`` models."""
        return self.get_jobs()


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
