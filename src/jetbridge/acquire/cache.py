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
