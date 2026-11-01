"""Raw-response blob store + fetch log + TTL cache.

Every response body is stored zstd-compressed, content-addressed by sha256.
This enables (a) re-parsing after a parser fix without re-fetching,
(b) turning real traffic into test fixtures, (c) an offline ``replay`` mode.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import zstandard
from sqlalchemy import select

from jetbridge.acquire.types import Outcome, Query, RawResponse
from jetbridge.paths import blob_dir
from jetbridge.store.db import FetchRow, session

_cctx = zstandard.ZstdCompressor(level=10)
_dctx = zstandard.ZstdDecompressor()


def put_blob(body: str) -> str:
    import hashlib

    data = body.encode("utf-8", "replace")
    sha = hashlib.sha256(data).hexdigest()
    path = blob_dir() / sha[:2] / f"{sha}.zst"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(_cctx.compress(data))
        tmp.replace(path)
    return sha


def get_blob(sha: str) -> str:
    path = blob_dir() / sha[:2] / f"{sha}.zst"
    return _dctx.decompress(path.read_bytes()).decode("utf-8", "replace")


def record_fetch(
    query: Query, raw: RawResponse | None, outcome: Outcome, detail: str | None = None
) -> str:
    fid = f"f_{uuid.uuid4().hex[:16]}"
    blob = put_blob(raw.body) if raw and raw.body else None
    with session() as s:
        s.add(
            FetchRow(
                id=fid,
                adapter=query.adapter,
                request_key=query.key,
                tier=raw.tier if raw else "T1",
                started_at=raw.started_at if raw else datetime.now(UTC),
                latency_ms=raw.latency_ms if raw else 0,
                http_status=raw.status if raw else None,
                outcome=outcome.value,
                detail=(detail or "")[:4000] or None,
                blob=blob,
                size=len(raw.body) if raw else 0,
            )
        )
    return fid


def cached(query: Query, max_age: timedelta) -> tuple[str, RawResponse] | None:
    """Most recent successful fetch for this exact query within ``max_age``."""
    cutoff = datetime.now(UTC) - max_age
    with session() as s:
        row = s.scalars(
            select(FetchRow)
            .where(
                FetchRow.request_key == query.key,
                FetchRow.outcome.in_([Outcome.OK.value, Outcome.EMPTY.value]),
                FetchRow.started_at >= cutoff,
                FetchRow.blob.is_not(None),
            )
            .order_by(FetchRow.started_at.desc())
            .limit(1)
        ).first()
    if row is None or row.blob is None:
        return None
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)
    return row.id, RawResponse(
        status=row.http_status,
        body=get_blob(row.blob),
        tier=row.tier,  # type: ignore[arg-type]
        started_at=started,
        latency_ms=row.latency_ms,
        meta={"cached": True},
    )


def cached_any(queries: list[Query], max_age: timedelta) -> tuple[str, RawResponse, Query] | None:
    """Most recent good answer to any of ``queries`` within ``max_age`` (see
    ``freshness.serving_queries``); returns the query it actually answered so the
    response is parsed with the party size it was fetched for."""
    by_key = {q.key: q for q in queries}
    cutoff = datetime.now(UTC) - max_age
    with session() as s:
        row = s.scalars(
            select(FetchRow)
            .where(
                FetchRow.request_key.in_(list(by_key)),
                FetchRow.outcome.in_([Outcome.OK.value, Outcome.EMPTY.value]),
                FetchRow.started_at >= cutoff,
                FetchRow.blob.is_not(None),
            )
            .order_by(FetchRow.started_at.desc())
            .limit(1)
        ).first()
    if row is None or row.blob is None:
        return None
    started = row.started_at if row.started_at.tzinfo else row.started_at.replace(tzinfo=UTC)
    raw = RawResponse(
        status=row.http_status,
        body=get_blob(row.blob),
        tier=row.tier,  # type: ignore[arg-type]
        started_at=started,
        latency_ms=row.latency_ms,
        meta={"cached": True},
    )
    return row.id, raw, by_key[row.request_key]


def fresh_index(max_age: timedelta = timedelta(hours=24)) -> dict[str, float]:
    """request_key -> age in seconds of its newest good answer (one query)."""
    from sqlalchemy import func

    cutoff = datetime.now(UTC) - max_age
    now = datetime.now(UTC)
    with session() as s:
        rows = s.execute(
            select(FetchRow.request_key, func.max(FetchRow.started_at))
            .where(
                FetchRow.outcome.in_([Outcome.OK.value, Outcome.EMPTY.value]),
                FetchRow.started_at >= cutoff,
                FetchRow.blob.is_not(None),
            )
            .group_by(FetchRow.request_key)
        ).all()
    out = {}
    for key, t in rows:
        if isinstance(t, str):
            t = datetime.fromisoformat(t)
        t = t if t.tzinfo else t.replace(tzinfo=UTC)
        out[key] = (now - t).total_seconds()
    return out


def freshest_age(queries: list[Query]) -> timedelta | None:
    """Age of the newest good answer to any of ``queries`` (no blob read)."""
    with session() as s:
        t = s.scalars(
            select(FetchRow.started_at)
            .where(
                FetchRow.request_key.in_([q.key for q in queries]),
                FetchRow.outcome.in_([Outcome.OK.value, Outcome.EMPTY.value]),
                FetchRow.blob.is_not(None),
            )
            .order_by(FetchRow.started_at.desc())
            .limit(1)
        ).first()
    if t is None:
        return None
    return datetime.now(UTC) - (t if t.tzinfo else t.replace(tzinfo=UTC))
