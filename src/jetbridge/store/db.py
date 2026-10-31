"""SQLite persistence (WAL). Append-only observations make the future monitor a diff.

Tables
------
searches        one row per user search (spec + status + progress)
ledger          one row per planned query: the coverage ledger
fetches         one row per network fetch (status, tier, latency, blob ref)
observations    normalized offers, append-only, keyed for diffing
options         solved multi-pax options for a search
verifications   live re-check results for options
kv              profile + misc settings
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from jetbridge.paths import db_path


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}  # noqa: RUF012


class SearchRow(Base):
    __tablename__ = "searches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    spec: Mapped[dict] = mapped_column(JSON)
    profile: Mapped[dict] = mapped_column(JSON)
    plan: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String, default="planned")  # planned|running|done|failed
    stage: Mapped[str | None] = mapped_column(String, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    report_path: Mapped[str | None] = mapped_column(String, nullable=True)
    worker_pid: Mapped[int | None] = mapped_column(Integer, nullable=True)


class LedgerRow(Base):
    __tablename__ = "ledger"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    search_id: Mapped[str] = mapped_column(ForeignKey("searches.id"), index=True)
    stage: Mapped[str] = mapped_column(String)  # calendar|day|seats|verify
    adapter: Mapped[str] = mapped_column(String)
    query_key: Mapped[str] = mapped_column(String)
    query: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="pending")
    # pending|ok|empty|blocked|failed|pruned|skipped
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    offers: Mapped[int] = mapped_column(Integer, default=0)
    fetch_id: Mapped[str | None] = mapped_column(String, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (Index("ix_ledger_search_key", "search_id", "query_key", unique=True),)


class FetchRow(Base):
    __tablename__ = "fetches"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    adapter: Mapped[str] = mapped_column(String, index=True)
    request_key: Mapped[str] = mapped_column(String, index=True)
    tier: Mapped[str] = mapped_column(String)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    latency_ms: Mapped[int] = mapped_column(Integer)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String)  # ok|blocked|rate_limited|error|empty
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    blob: Mapped[str | None] = mapped_column(String, nullable=True)
    size: Mapped[int] = mapped_column(Integer, default=0)


class ObservationRow(Base):
    __tablename__ = "observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    search_id: Mapped[str | None] = mapped_column(String, index=True, nullable=True)
    fetch_id: Mapped[str] = mapped_column(String)
    kind: Mapped[str] = mapped_column(String)  # award|cash
    source: Mapped[str] = mapped_column(String)  # program id or cash source
    origin: Mapped[str] = mapped_column(String(3))
    destination: Mapped[str] = mapped_column(String(3))
    depart_date: Mapped[str] = mapped_column(String(10))
    flight_key: Mapped[str] = mapped_column(String)
    cabin: Mapped[str] = mapped_column(String(1))
    miles: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cash: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str] = mapped_column(String(3))
    seats_kind: Mapped[str | None] = mapped_column(String, nullable=True)
    seats: Mapped[int | None] = mapped_column(Integer, nullable=True)
    offer: Mapped[dict] = mapped_column(JSON)

    __table_args__ = (
        Index("ix_obs_route", "source", "origin", "destination", "depart_date", "cabin"),
        Index("ix_obs_flight", "flight_key", "source", "cabin"),
    )


class OptionRow(Base):
    __tablename__ = "options"
    id: Mapped[str] = mapped_column(String, primary_key=True)
    search_id: Mapped[str] = mapped_column(ForeignKey("searches.id"), index=True)
    rank: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String, default="unverified")


class VerificationRow(Base):
    __tablename__ = "verifications"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    option_id: Mapped[str] = mapped_column(String, index=True)
    offer_id: Mapped[str] = mapped_column(String)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    status: Mapped[str] = mapped_column(String)  # confirmed|changed|gone|unverifiable
    detail: Mapped[dict] = mapped_column(JSON)


class KVRow(Base):
    __tablename__ = "kv"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text)


_engines: dict[str, Engine] = {}


def engine(path: Path | None = None) -> Engine:
    p = str(path or db_path())
    if p not in _engines:
        eng = create_engine(f"sqlite:///{p}", future=True)

        @event.listens_for(eng, "connect")
        def _pragmas(dbapi_conn, _rec):  # type: ignore[no-untyped-def]
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=10000")
            cur.close()

        Base.metadata.create_all(eng)
        _engines[p] = eng
    return _engines[p]


@contextmanager
def session(path: Path | None = None) -> Iterator[Session]:
    maker = sessionmaker(engine(path), expire_on_commit=False)
    with maker() as s:
        try:
            yield s
            s.commit()
        except BaseException:
            s.rollback()
            raise


def kv_get(key: str, default: object = None) -> object:
    with session() as s:
        row = s.get(KVRow, key)
        return json.loads(row.value) if row else default


def kv_set(key: str, value: object) -> None:
    with session() as s:
        s.merge(KVRow(key=key, value=json.dumps(value, default=str)))
