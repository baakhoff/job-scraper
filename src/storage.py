"""Persistence layer: store parsed listings in SQLite via SQLAlchemy.

Uses the SQLAlchemy 2.0 declarative API. The schema is intentionally close
to ``models.JobListing``; ``job_id`` is the natural primary key so re-runs
upsert rather than duplicate.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import String, create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .models import JobListing


class Base(DeclarativeBase):
    """Declarative base for ORM models."""


class JobRecord(Base):
    """ORM row mirroring a :class:`~src.models.JobListing`."""

    __tablename__ = "job_listings"

    job_id: Mapped[str] = mapped_column(String, primary_key=True)
    title: Mapped[str] = mapped_column(String)
    company: Mapped[str] = mapped_column(String)
    location: Mapped[str | None] = mapped_column(String, nullable=True)
    url: Mapped[str | None] = mapped_column(String, nullable=True)


class Storage:
    """Thin wrapper around a SQLAlchemy engine for reading/writing listings."""

    def __init__(self, db_path: str) -> None:
        self.engine: Engine = create_engine(f"sqlite:///{db_path}")

    def init_db(self) -> None:
        """Create tables if they do not exist.

        Stub: ``Base.metadata.create_all(self.engine)``.
        """
        raise NotImplementedError

    def upsert_many(self, listings: Iterable[JobListing]) -> int:
        """Insert or update listings keyed by ``job_id``; return rows written.

        Stub: open a session, map each ``JobListing`` to a ``JobRecord``, and
        merge.
        """
        raise NotImplementedError

    def all_listings(self) -> list[JobListing]:
        """Read every stored listing back as ``JobListing`` models.

        Stub.
        """
        raise NotImplementedError
