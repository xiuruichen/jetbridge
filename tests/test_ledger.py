"""The ledger of what searches showed, and the per-flight release timing learned from it."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from conftest import DAY, add_cx, answer_from, connecting_slice, cx_slice, make_runner, response

from jetbridge import ledger
from jetbridge.alerts import DateState
from jetbridge.models import AnswerKind, SearchAnswer
from jetbridge.predictor import TIGHT_S, flight_patterns

HOUR = 3600.0


def record(store, offers, at, cached=None):
    return store.record_offers("american", "JFK", "HKG", DAY, 1, offers, at, cached)


# ---- what an answer contains ------------------------------------------------------------
def test_offers_are_the_cheapest_per_flight_and_cabin():
    parsed = answer_from(response(cx_slice(), connecting_slice())).parsed
    offers = ledger.offers_in(parsed)
    assert offers[("CX805", "business")][0] == 110_000
    assert offers[("CX805", "economy")][0] == 60_000
    assert offers[("AA100+CX806", "business")][0] == 80_000


def test_only_a_whole_readable_answer_counts_as_a_snapshot():
    assert ledger.complete(answer_from(response(cx_slice())).parsed)
    assert not ledger.complete(answer_from(response(cx_slice(), error="309")).parsed)
    assert not ledger.complete(None)


def test_american_says_whether_an_answer_was_cached():
    assert answer_from(response(cx_slice())).parsed.cached is False


# ---- the events -------------------------------------------------------------------------
def test_the_first_snapshot_is_a_baseline_and_writes_no_events(store):
    assert record(store, {("CX805", "business"): (110_000, 2)}, 1000.0) == 0
    assert store.release_events("american", TIGHT_S) == []


def test_a_flight_appearing_vanishing_and_changing_price_each_write_an_event(store):
    record(store, {("CX805", "business"): (110_000, 2)}, 0.0)
    assert (
        record(
            store, {("CX805", "business"): (110_000, 2), ("CX889", "business"): (95_000, 1)}, HOUR
        )
        == 1
    )
    assert (
        record(
            store,
            {("CX805", "business"): (120_000, 2), ("CX889", "business"): (95_000, 1)},
            2 * HOUR,
        )
        == 1
    )
    assert record(store, {("CX889", "business"): (95_000, 1)}, 3 * HOUR) == 1
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM offer_events ORDER BY id")]
    assert kinds == ["appeared", "price", "vanished"]
    assert record(store, {("CX889", "business"): (95_000, 1)}, 4 * HOUR) == 0  # nothing changed


def test_a_release_is_only_placed_when_the_answer_before_was_recent(store):
    record(store, {}, 0.0)
    record(store, {("CX805", "business"): (110_000, 2)}, HOUR)  # one hour after a clear answer
    record(store, {}, 2 * HOUR)  # gone again
    record(store, {("CX889", "business"): (95_000, 1)}, 20 * HOUR)  # 18 hours of not knowing
    events = store.release_events("american", TIGHT_S)
    assert [(f, c) for f, c, _ in events] == [("CX805", "business")]


def test_other_programs_and_dates_keep_their_own_snapshots(store):
    record(store, {("CX805", "business"): (110_000, 2)}, 0.0)
    store.record_offers("delta", "JFK", "HKG", DAY, 1, {}, 0.0)
    assert (
        store.record_offers("delta", "JFK", "HKG", DAY, 1, {("DL1", "business"): (1, 1)}, HOUR) == 1
    )
    assert record(store, {("CX805", "business"): (110_000, 2)}, HOUR) == 0


def test_old_events_and_past_dates_are_pruned(store):
    record(store, {}, 0.0)
    record(store, {("CX805", "business"): (110_000, 2)}, HOUR)
    store.prune_offers(10 * HOUR, DAY + timedelta(days=1))
    assert store.conn.execute("SELECT COUNT(*) FROM offer_events").fetchone()[0] == 0
    assert store.conn.execute("SELECT COUNT(*) FROM offer_state").fetchone()[0] == 0


# ---- the learned timing -----------------------------------------------------------------
def at(day: int, hour: int) -> float:
    return datetime(2027, 1, day, hour, 15).timestamp()


def test_a_flight_with_enough_openings_in_one_window_gets_a_pattern():
    events = [("CX845", "business", at(d, 2)) for d in range(1, 6)]
    events += [("CX805", "business", at(d, 2 + 4 * d)) for d in range(1, 4)]  # too few and spread
    found = flight_patterns(events)
    assert set(found) == {("CX845", "business")}
    assert found[("CX845", "business")].count == 5


async def test_a_tick_writes_what_it_saw_and_learns_the_flights_window(store, clock):
    add_cx(store, clock)
    shut = answer_from(response(cx_slice(business_miles=None)))
    runner, _, _, _ = make_runner(store, clock, [shut])
    await runner.tick()
    assert store.conn.execute("SELECT COUNT(*) FROM offer_events").fetchone()[0] == 0  # baseline
    assert store.conn.execute("SELECT COUNT(*) FROM offer_state").fetchone()[0] == 1  # economy
    opened = answer_from(response(cx_slice()))
    runner, _, _, _ = make_runner(store, clock, [opened, opened])
    clock.now += HOUR  # the answer before it is recent enough to place the release
    store.set_next_check(1, clock.now)
    await runner.tick()
    events = store.release_events("american", TIGHT_S)
    assert [(f, c) for f, c, _ in events] == [("CX805", "business")]


async def test_an_unreadable_answer_writes_nothing(store, clock):
    add_cx(store, clock)
    broken = answer_from(response(cx_slice(), error="309"))
    runner, _, _, _ = make_runner(store, clock, [broken])
    await runner.tick()
    assert store.conn.execute("SELECT COUNT(*) FROM offer_state").fetchone()[0] == 0
    gone = SearchAnswer(AnswerKind.ERROR, detail="x")
    runner._record(
        __import__("jetbridge.models", fromlist=["Query"]).Query("JFK", "HKG", DAY), gone
    )
    assert store.conn.execute("SELECT COUNT(*) FROM offer_state").fetchone()[0] == 0


def test_a_flights_own_window_beats_the_programs(store, clock):
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock)
    store.set("release_pattern:american", json.dumps({"start_hour": 8, "count": 5, "total": 6}))
    assert runner._best_pattern(watch).start_hour == 8
    store.set(
        "release_pattern:american:CX805:business",
        json.dumps({"start_hour": 2, "count": 5, "total": 5}),
    )
    assert runner._best_pattern(watch).start_hour == 2
    any_watch = store.add_watch(
        __import__("jetbridge.models", fromlist=["Flight"]).Flight("ANY", ""),
        "JFK",
        "HKG",
        (DAY,),
        watch.cabin,
        now=clock(),
    )
    assert runner._best_pattern(any_watch).start_hour == 8  # no flight to be specific about


def test_the_digest_reports_flight_timing_and_cached_answers(store, clock):
    store.set("digest_enabled", "1")
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock)
    store.set(
        "release_pattern:american:CX805:business",
        json.dumps({"start_hour": 1, "count": 5, "total": 6}),
    )
    store.set("cached_seen:american", json.dumps([3, 10]))
    _, body = runner.build_digest([watch])
    assert (
        "Release timing for CX805: 5 of 6 past openings were first seen between 1 AM and 4 AM"
        in body
    )
    assert "3 of 10 aa.com answers were served from the site's own cache" in body
    assert DateState().status is None  # keeps the import used


def test_different_party_sizes_keep_separate_snapshots(store):
    one = {("CX805", "business"): (110_000, 2)}
    four = {}  # a search for four hides flights with only two seats
    store.record_offers("american", "JFK", "HKG", DAY, 1, one, 0.0)
    store.record_offers("american", "JFK", "HKG", DAY, 4, four, 10.0)
    assert store.record_offers("american", "JFK", "HKG", DAY, 1, one, HOUR) == 0
    assert store.record_offers("american", "JFK", "HKG", DAY, 4, four, HOUR) == 0
    assert store.conn.execute("SELECT COUNT(*) FROM offer_events").fetchone()[0] == 0
