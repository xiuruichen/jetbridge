"""Rebuild the availability store from every stored raw response (no network).

Every fetch since day one kept its raw body, so the store can be seeded (or
rebuilt after a parser fix) from history: oldest first, so the final cells are
the newest answers and the alerts reflect real changes over time."""

from __future__ import annotations

from datetime import UTC, date

from sqlalchemy import delete, select

from jetbridge.acquire.adapters import registry
from jetbridge.acquire.cache import get_blob
from jetbridge.acquire.types import Query, RawResponse
from jetbridge.models.core import Cabin, Provenance
from jetbridge.store import availability
from jetbridge.store.db import AlertRow, AvailabilityRow, FetchRow, session


def query_from_key(key: str) -> Query | None:
    """Inverse of ``Query.key``: ``adapter:kind:O-D:date:pN:cabin``."""
    try:
        adapter, kind, od, day, pax, cabin = key.split(":")
        o, d = od.split("-")
        return Query(
            adapter,
            kind,  # type: ignore[arg-type]
            o,
            d,
            date.fromisoformat(day),
            int(pax.lstrip("p")),
            None if cabin == "-" else Cabin(cabin),
        )
    except (ValueError, KeyError):
        return None


def backfill(*, reset: bool = True, keep_alerts: bool = False) -> dict[str, int]:
    adapters = registry()
    award_ids = [a.id for a in adapters.values() if a.program]
    counts = {"fetches": 0, "ok": 0, "empty": 0, "skipped": 0, "alerts": 0}
    if reset:
        with session() as s:
            s.execute(delete(AvailabilityRow))
            if not keep_alerts:
                s.execute(delete(AlertRow))
    with session() as s:
        rows = list(
            s.execute(
                select(
                    FetchRow.id,
                    FetchRow.request_key,
                    FetchRow.blob,
                    FetchRow.started_at,
                    FetchRow.tier,
                    FetchRow.http_status,
                )
                .where(
                    FetchRow.adapter.in_(award_ids),
                    FetchRow.outcome.in_(["ok", "empty"]),
                    FetchRow.blob.is_not(None),
                )
                .order_by(FetchRow.started_at)
            )
        )
    for fid, key, blob, started, tier, status in rows:
        counts["fetches"] += 1
        q = query_from_key(key)
        if q is None or q.adapter not in adapters:
            counts["skipped"] += 1
            continue
        ad = adapters[q.adapter]
        t = started if started.tzinfo else started.replace(tzinfo=UTC)
        raw = RawResponse(status=status, body=get_blob(blob), tier=tier, started_at=t, latency_ms=0)
        prov = Provenance(adapter=ad.id, tier=tier, fetch_id=fid, observed_at=t, request_pax=q.pax)
        try:
            parsed = ad.parse(raw, q, prov)
        except Exception:
            counts["skipped"] += 1
            continue
        empty = parsed.empty and ad.is_empty_answer(raw)
        if parsed.empty and not empty:
            counts["skipped"] += 1
            continue
        alerts = availability.ingest(
            q, ad.program, parsed, empty=empty, fetch_id=fid, observed_at=t
        )
        counts["alerts"] += len(alerts)
        counts["empty" if empty else "ok"] += 1
    return counts
