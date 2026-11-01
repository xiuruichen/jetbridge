"""The crawler: a slow, steady background refresh of watched routes.

This is how seats.aero can answer instantly: searches read a store that a
crawler keeps fresh, instead of bursting hundreds of live requests per search.
Here the crawler:

1. expands every active watch with the planner (deep mode: nothing deferred),
2. normalizes queries (Delta/Alaska at 1 pax: one answer serves any party
   size, see ``freshness.serving_queries``) and de-duplicates across watches,
3. selects what is *due*: never fetched, or older than ``REFRESH_AT`` of its
   freshness TTL; queries that keep erroring are backed off,
4. runs them most-urgent first through the normal engine with role
   ``crawl``, so the rate governor caps it at half of each site's window
   budget (searches keep headroom), and a block cools the whole site down for
   every process,
5. folds each answer into the availability store, which emits change alerts.

It never solves challenges. With ``--show-browser`` a human can clear one.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select

from jetbridge.acquire.adapters import registry
from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.cache import fresh_index
from jetbridge.acquire.engine import Engine
from jetbridge.acquire.freshness import serving_queries, ttl_for
from jetbridge.acquire.types import Query
from jetbridge.crawl.watch import active_watches
from jetbridge.kb import KB, load_kb
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.plan.planner import build_plan
from jetbridge.store.db import AlertRow, FetchRow, KVRow, session

log = logging.getLogger("jetbridge.crawl")

REFRESH_AT = 0.8  # refresh when an answer reaches 80% of its TTL
ERROR_BACKOFF = timedelta(hours=6)
ERROR_LIMIT = 2  # this many errors on a query inside the backoff window: skip it
LOCK_KEY = "crawler:lock"
STALE_LOCK_S = 600


@dataclass
class CrawlItem:
    query: Query
    priority: float
    urgency: float  # age / ttl (1000 = never fetched)
    watches: set[str] = field(default_factory=set)
    # the watches' own (un-normalized) queries: a fresh answer to any of them
    # also counts, e.g. a 3-pax Delta answer from a search
    originals: dict[str, Query] = field(default_factory=dict)


def normalize(q: Query, adapter: Adapter) -> Query:
    """Crawl price-independent, exact-seat adapters at 1 pax: that answer serves
    every party size (and shows 1-2 seat space a k-pax search would hide)."""
    if adapter.price_independent_of_pax and adapter.capabilities.seat_counts == "exact":
        return replace(q, pax=1)
    return q


def _recent_errors() -> dict[str, int]:
    since = datetime.now(UTC) - ERROR_BACKOFF
    with session() as s:
        rows = s.execute(
            select(FetchRow.request_key, func.count(FetchRow.id))
            .where(FetchRow.outcome == "error", FetchRow.started_at >= since)
            .group_by(FetchRow.request_key)
        ).all()
    return {k: n for k, n in rows}


def due_items(
    adapters: dict[str, Adapter],
    kb: KB,
    *,
    today: date | None = None,
    refresh_at: float = REFRESH_AT,
) -> tuple[list[CrawlItem], dict[str, int]]:
    """What the crawler should fetch now, most urgent first; plus counters."""
    today = today or date.today()
    items: dict[str, CrawlItem] = {}
    for w in active_watches():
        spec = SearchSpec.model_validate(w.spec)
        profile = Profile.model_validate(w.profile)
        plan = build_plan(spec, profile, kb, adapters, deep=True, use_cache=False)
        for pq in plan.queries:
            if pq.skip_reason is not None or pq.query.date < today:
                continue
            ad = adapters[pq.query.adapter]
            q = normalize(pq.query, ad)
            it = items.get(q.key)
            prio = pq.priority * w.weight
            if it is None:
                items[q.key] = it = CrawlItem(q, prio, 0.0)
            it.priority = max(it.priority, prio)
            it.watches.add(w.id)
            it.originals[pq.query.key] = pq.query
    fresh = fresh_index()
    errors = _recent_errors()
    failures = _failures()
    counts = {"planned": len(items), "fresh": 0, "backed_off": 0, "due": 0}
    due: list[CrawlItem] = []
    for it in items.values():
        ad = adapters[it.query.adapter]
        ttl = ttl_for(it.query, ad, today=today).total_seconds()
        serving = {k.key for k in serving_queries(it.query, ad)}
        for oq in it.originals.values():
            serving |= {k.key for k in serving_queries(oq, ad)}
        ages = [fresh[k] for k in serving if k in fresh]
        if ages and min(ages) < ttl * refresh_at:
            counts["fresh"] += 1
            continue
        if (
            errors.get(it.query.key, 0) >= ERROR_LIMIT
            or failures.get(it.query.key, 0) >= ERROR_LIMIT
        ):
            counts["backed_off"] += 1
            continue
        it.urgency = 1000.0 if not ages else min(ages) / ttl
        due.append(it)
    due.sort(key=lambda i: (-i.urgency, -i.priority, i.query.date))
    counts["due"] = len(due)
    return due, counts


FAIL_KEY = "crawler:failures"


def _failures() -> dict[str, int]:
    """Queries whose answers keep failing to parse (e.g. aa.com error 309), within
    ERROR_BACKOFF: the fetch itself succeeded, so ``_recent_errors`` misses them."""
    with session() as s:
        row = s.get(KVRow, FAIL_KEY)
        d = json.loads(row.value) if row else {}
    cutoff = time.time() - ERROR_BACKOFF.total_seconds()
    return {k: n for k, (n, t) in d.items() if t >= cutoff}


def _note_result(key: str, status: str) -> None:
    with session() as s:
        row = s.get(KVRow, FAIL_KEY)
        d = json.loads(row.value) if row else {}
        cutoff = time.time() - ERROR_BACKOFF.total_seconds()
        d = {k: v for k, v in d.items() if v[1] >= cutoff}
        if status == "failed":
            n = d.get(key, [0, 0])[0] + 1
            d[key] = [n, time.time()]
        elif status in ("ok", "empty"):
            d.pop(key, None)
        else:
            return
        s.merge(KVRow(key=FAIL_KEY, value=json.dumps(d)))


# ---------------------------------------------------------------------- lock
def _take_lock() -> bool:
    now = time.time()
    with session() as s:
        row = s.get(KVRow, LOCK_KEY)
        if row is not None:
            d = json.loads(row.value)
            if d.get("pid") != os.getpid() and now - d.get("heartbeat", 0) < STALE_LOCK_S:
                return False
        s.merge(KVRow(key=LOCK_KEY, value=json.dumps({"pid": os.getpid(), "heartbeat": now})))
    return True


def _heartbeat(status: dict) -> None:
    with session() as s:
        s.merge(
            KVRow(
                key=LOCK_KEY,
                value=json.dumps(
                    {"pid": os.getpid(), "heartbeat": time.time(), **status}, default=str
                ),
            )
        )


def _release_lock() -> None:
    with session() as s:
        row = s.get(KVRow, LOCK_KEY)
        if row is not None and json.loads(row.value).get("pid") == os.getpid():
            s.delete(row)


def crawler_status() -> dict | None:
    with session() as s:
        row = s.get(KVRow, LOCK_KEY)
        if row is None:
            return None
        d = json.loads(row.value)
    d["alive"] = time.time() - d.get("heartbeat", 0) < STALE_LOCK_S
    return d


# ---------------------------------------------------------------------- run
async def crawl(
    *,
    minutes: float | None = None,
    once: bool = False,
    offscreen: bool = True,
    max_queries: int | None = None,
    broker=None,  # type: ignore[no-untyped-def]  # injected in tests
    adapters: dict[str, Adapter] | None = None,
    idle_sleep_s: float = 300.0,
) -> dict:
    """Run the crawler. ``minutes`` bounds wall-clock time (requests that cannot
    start in time are left for the next run); ``once`` stops after one pass."""
    if not _take_lock():
        raise RuntimeError("another crawler is running (see `jetbridge crawl --status`)")
    kb = load_kb()
    adapters = adapters or registry()
    end = None if minutes is None else time.monotonic() + minutes * 60
    totals: dict[str, int] = defaultdict(int)
    started = datetime.now(UTC)
    alerts_before = _alert_count()
    try:
        if broker is None:
            from jetbridge.acquire.broker import SessionBroker

            async with SessionBroker(offscreen=offscreen) as br:
                await _loop(br, adapters, kb, end, once, max_queries, totals, idle_sleep_s, started)
        else:
            await _loop(broker, adapters, kb, end, once, max_queries, totals, idle_sleep_s, started)
    finally:
        _release_lock()
    totals["alerts"] = _alert_count() - alerts_before
    return dict(totals)


async def _loop(broker, adapters, kb, end, once, max_queries, totals, idle_sleep_s, started):  # type: ignore[no-untyped-def]
    engine = Engine(adapters, broker, role="crawl", max_attempts=2, deadline=end)
    budget_left = max_queries
    while True:
        if end is not None and time.monotonic() >= end:
            break
        due, counts = due_items(adapters, kb)
        if budget_left is not None:
            due = due[:budget_left]
        log.info("crawl cycle: %s", counts)
        _heartbeat({"started": started, "cycle": counts, "totals": dict(totals)})
        if due:
            done = await _run_items(engine, due, totals, started)
            if budget_left is not None:
                budget_left -= done
                if budget_left <= 0:
                    break
        if once:
            break
        if not due:
            wait = idle_sleep_s
            if end is not None:
                wait = min(wait, max(0.0, end - time.monotonic()))
            if wait <= 0:
                break
            await asyncio.sleep(wait)


async def _run_items(engine: Engine, items: list[CrawlItem], totals: dict, started) -> int:  # type: ignore[no-untyped-def]
    by_site: dict[str, list[CrawlItem]] = defaultdict(list)
    for it in items:
        by_site[engine.adapters[it.query.adapter].site].append(it)
    ran = 0

    async def worker(queue: list[CrawlItem]) -> None:
        nonlocal ran
        while queue:
            it = queue.pop(0)
            res = await engine.run(it.query, use_cache=False)
            totals[res.status] += 1
            _note_result(it.query.key, res.status)
            ran += 1
            if res.status == "deferred":
                queue.clear()  # this site is cooling / out of budget until the deadline
            if ran % 10 == 0:
                _heartbeat({"started": started, "totals": dict(totals)})

    tasks = []
    for site, q in by_site.items():
        n = max(1, engine.scheduler.policy(site).concurrency)
        tasks += [worker(q) for _ in range(n)]
    await asyncio.gather(*tasks)
    return ran


def _alert_count() -> int:
    with session() as s:
        return int(s.scalar(select(func.count(AlertRow.id))) or 0)
