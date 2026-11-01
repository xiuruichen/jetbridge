"""Cache-first search: freshness, party-size dominance, availability store, alerts,
cache-aware planning and the crawler's due selection."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from jetbridge.acquire.adapters import registry
from jetbridge.acquire.cache import record_fetch
from jetbridge.acquire.engine import Engine
from jetbridge.acquire.freshness import serving_queries, ttl_for
from jetbridge.acquire.scheduler import Scheduler
from jetbridge.acquire.types import Outcome, Query, RawResponse

FIX = Path(__file__).resolve().parents[1] / "fixtures"
DAY = date(2027, 5, 28)


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_VAR", str(tmp_path))
    monkeypatch.setenv("JETBRIDGE_NO_AUTOWATCH", "1")
    from jetbridge.store import db

    db._engines.clear()


def _raw(body: str, at: datetime | None = None) -> RawResponse:
    return RawResponse(
        status=200, body=body, tier="T1", started_at=at or datetime.now(UTC), latency_ms=5
    )


def _delta_body() -> str:
    return "[" + (FIX / "delta" / "dtw_icn_award_20270528.json").read_text(encoding="utf-8") + "]"


def _united_body() -> str:
    return (FIX / "united" / "ord_hnd_award_20270528.sse").read_text(encoding="utf-8")


def _store(q: Query, body: str, at: datetime | None = None) -> str:
    """Record + interpret a response as if it had been fetched live."""
    ad = registry()
    raw = _raw(body, at)
    fid = record_fetch(q, raw, Outcome.OK)
    eng = Engine(ad, broker=None, scheduler=Scheduler(fast=True))
    res = eng._interpret(ad[q.adapter], q, raw, fid)
    assert res.status in ("ok", "empty"), res.reason
    return fid


def test_ttl_is_longer_far_from_departure():
    ad = registry()
    today = date(2027, 1, 1)
    far = Query("united", "day", "ORD", "HND", date(2027, 11, 1), 1)
    near = Query("united", "day", "ORD", "HND", date(2027, 1, 2), 1)
    assert ttl_for(far, ad["united"], today=today) == timedelta(hours=24)
    assert ttl_for(near, ad["united"], today=today) == timedelta(minutes=45)
    cash = Query("google_flights", "day", "CVG", "HKG", date(2027, 11, 1), 3)
    assert ttl_for(cash, ad["google_flights"], today=today) == timedelta(hours=12)


def test_party_size_dominance_only_for_verified_adapters():
    ad = registry()
    q3 = Query("delta", "day", "DTW", "ICN", DAY, 3)
    assert {q.pax for q in serving_queries(q3, ad["delta"])} == {1, 2, 3}
    u3 = Query("united", "day", "ORD", "HND", DAY, 3)
    assert [q.pax for q in serving_queries(u3, ad["united"])] == [3]


async def test_one_pax_delta_answer_serves_three_pax_query_without_network():
    _store(Query("delta", "day", "DTW", "ICN", DAY, 1), _delta_body())
    eng = Engine(registry(), broker=None, scheduler=Scheduler(fast=True))
    res = await eng.run(Query("delta", "day", "DTW", "ICN", DAY, 3))
    assert res.status == "ok" and res.from_cache
    assert "1-pax answer" in res.reason
    # provenance keeps the party size the answer was fetched for (solver caps by seats)
    assert all(o.provenance.request_pax == 1 for o in res.parsed.awards)


async def test_united_one_pax_answer_does_not_serve_three_pax():
    _store(Query("united", "day", "ORD", "HND", DAY, 1), _united_body())
    eng = Engine(registry(), broker=None, scheduler=Scheduler(fast=True))
    res = await eng.run(Query("united", "day", "ORD", "HND", DAY, 3))
    assert res.status == "failed" and not res.from_cache  # would need a live fetch


async def test_stale_answer_is_not_served():
    old = datetime.now(UTC) - timedelta(days=3)
    _store(Query("delta", "day", "DTW", "ICN", DAY, 1), _delta_body(), at=old)
    eng = Engine(registry(), broker=None, scheduler=Scheduler(fast=True))
    assert (await eng.run(Query("delta", "day", "DTW", "ICN", DAY, 1))).from_cache is False


def test_availability_cells_and_change_alerts():
    from sqlalchemy import select

    from jetbridge.acquire.types import ParseResult
    from jetbridge.store import availability
    from jetbridge.store.db import AvailabilityRow, session

    q = Query("delta", "day", "DTW", "ICN", DAY, 1)
    t0 = datetime.now(UTC) - timedelta(hours=2)
    _store(q, _delta_body(), at=t0)
    with session() as s:
        cells = list(s.scalars(select(AvailabilityRow)))
    assert cells and all(c.status == "available" for c in cells)
    j = next(c for c in cells if c.cabin == "J")
    assert j.min_miles and j.max_seats and j.seats_kind in ("exact", "lower_bound") and j.carriers

    # the same route later answers "no flights": every cabin is reported gone
    alerts = availability.ingest(
        q, "delta", ParseResult(), empty=True, fetch_id="f_x", observed_at=datetime.now(UTC)
    )
    assert {a.kind for a in alerts} == {"gone"}
    assert {a.cabin for a in alerts} == {c.cabin for c in cells}
    # an OLDER answer replayed later never overwrites the newer snapshot
    assert (
        availability.ingest(q, "delta", ParseResult(), empty=True, fetch_id="f_y", observed_at=t0)
        == []
    )

    # space returns: new_space alerts
    _store(q, _delta_body(), at=datetime.now(UTC) + timedelta(seconds=1))
    kinds = {a["kind"] for a in availability.recent_alerts(50)}
    assert {"gone", "new_space"} <= kinds


def test_cached_search_respects_party_size_rules():
    from jetbridge.store.availability import search

    _store(Query("delta", "day", "DTW", "ICN", DAY, 1), _delta_body())
    _store(Query("united", "day", "ORD", "HND", DAY, 1), _united_body())
    one = search(["DTW", "ORD"], ["ICN", "HND"], DAY, DAY, pax=1)
    assert {r["program"] for r in one} == {"delta", "united"}
    three = search(["DTW", "ORD"], ["ICN", "HND"], DAY, DAY, pax=3)
    # united 1-pax prices are not valid for 3; delta cells count only with >= 3 exact seats
    assert all(r["program"] == "delta" and r["seats"] >= 3 for r in three)


def test_planner_serves_fresh_queries_from_cache_without_budget():
    from jetbridge.kb import load_kb
    from jetbridge.models.spec import Profile, SearchSpec
    from jetbridge.plan.planner import build_plan

    spec = SearchSpec.model_validate(
        {
            "origins": ["DTW"],
            "destinations": [{"code": "ICN", "weight": 1.0}],
            "outbound": {"start": "2027-05-28", "end": "2027-05-28"},
            "passengers": 3,
            "programs": ["delta"],
            "include_cash": False,
        }
    )
    before = build_plan(spec, Profile(), load_kb(), registry())
    assert before.summary()["queries_cached"] == 0
    _store(Query("delta", "day", "DTW", "ICN", DAY, 1), _delta_body())
    after = build_plan(spec, Profile(), load_kb(), registry())
    sm = after.summary()
    assert sm["queries_cached"] == 1 and sm["live_by_adapter"] == {}


async def test_crawler_due_selection_and_one_pass():
    from jetbridge.crawl.crawler import crawl, due_items
    from jetbridge.crawl.watch import add_watch, list_watches
    from jetbridge.kb import load_kb
    from jetbridge.models.spec import Profile, SearchSpec

    spec = SearchSpec.model_validate(
        {
            "origins": ["DTW"],
            "destinations": [{"code": "ICN", "weight": 1.0}],
            "outbound": {"start": "2027-05-27", "end": "2027-05-28"},
            "passengers": 3,
            "programs": ["delta"],
            "include_cash": False,
        }
    )
    wid, created = add_watch(spec, Profile())
    assert created and add_watch(spec, Profile()) == (wid, False)  # de-duplicated
    assert len(list_watches()) == 1

    ad = registry()
    due, counts = due_items(ad, load_kb(), today=date(2027, 1, 1))
    assert counts["due"] == 2 and all(i.query.pax == 1 for i in due)  # delta crawled at 1 pax

    class Broker:
        offscreen = True

        async def page(self, site, url, settle_ms=0):
            return object()

        async def rotate(self, site):
            pass

        async def wait_for_human(self, site, timeout_s=0):
            return False

    delta = ad["delta"]
    body = _delta_body()

    async def fake_fetch(page, q):
        return _raw(body)

    delta.fetch = fake_fetch  # type: ignore[method-assign]
    totals = await crawl(once=True, broker=Broker(), adapters=ad)
    assert totals.get("ok", 0) + totals.get("failed", 0) == 2
    _due2, counts2 = due_items(ad, load_kb(), today=date(2027, 1, 1))
    assert counts2["fresh"] >= 1  # answered queries are no longer due


def test_crawler_backs_off_queries_that_keep_failing_to_parse():
    from jetbridge.crawl.crawler import ERROR_LIMIT, _failures, _note_result

    key = "american:day:CVG-HKG:2027-05-24:p3:-"
    for _ in range(ERROR_LIMIT):
        _note_result(key, "failed")
    assert _failures()[key] == ERROR_LIMIT
    _note_result(key, "ok")
    assert key not in _failures()
