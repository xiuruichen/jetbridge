"""How long an answer stays fresh, and which stored answers can serve a query.

Award space far from departure moves slowly (saver releases are batchy and
mostly happen at schedule open and inside ~2 weeks), so like seats.aero we keep
far-out answers longer and close-in answers briefly. Verification of shortlisted
options always goes live (``use_cache=False``), so a cached answer can shape the
search but never becomes a booking recommendation unchecked.

Party-size dominance: for an adapter verified ``price_independent_of_pax`` with
exact seat counts (Delta, Alaska), a search for p' passengers lists every fare
with >= p' seats at the same per-person price, so an answer for p' <= p serves a
p-passenger query: the solver still caps each offer at its exact seat count. For
pax-dependent adapters (United, AA) only the exact party size counts.
"""

from __future__ import annotations

from datetime import date, timedelta

from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.types import Query

# (max days to departure, ttl)
_TTL_STEPS: list[tuple[int, timedelta]] = [
    (3, timedelta(minutes=45)),
    (14, timedelta(hours=2)),
    (60, timedelta(hours=6)),
    (180, timedelta(hours=12)),
    (10_000, timedelta(hours=24)),
]
CASH_TTL_FACTOR = 0.5  # cash fares move faster than award space


def ttl_for(q: Query, adapter: Adapter | None = None, *, today: date | None = None) -> timedelta:
    days = (q.date - (today or date.today())).days
    ttl = next(t for limit, t in _TTL_STEPS if days <= limit)
    if adapter is not None and adapter.capabilities.cash and not adapter.capabilities.award:
        ttl = ttl * CASH_TTL_FACTOR
    return ttl


def serving_queries(q: Query, adapter: Adapter) -> list[Query]:
    """Queries whose stored answer is a valid answer to ``q`` (``q`` first)."""
    out = [q]
    if adapter.price_independent_of_pax and adapter.capabilities.seat_counts == "exact":
        out += [_with_pax(q, p) for p in range(1, q.pax)]
    return out


def _with_pax(q: Query, pax: int) -> Query:
    from dataclasses import replace

    return replace(q, pax=pax)
