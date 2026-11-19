"""A flexible scope: nearby dates and other airports, scouted with leftover searches only."""

from __future__ import annotations

from datetime import timedelta

import pytest
from conftest import DAY, add_cx, answer_from, cx_slice, make_runner, response
from typer.testing import CliRunner

from jetbridge import flex
from jetbridge.cli import app
from jetbridge.models import Query
from jetbridge.scheduler import plan_scouts


def _flexible(store, clock, **scope):
    watch = add_cx(store, clock)
    store.set_flex(
        watch.id, scope.get("days", 0), scope.get("origins", ()), scope.get("destinations", ())
    )
    return store.get_watch(watch.id)


# ---- expanding names --------------------------------------------------------------------
def test_a_region_becomes_its_airports():
    assert "YYZ" in flex.expand("north-america") and "JFK" in flex.expand("North America")


def test_codes_and_regions_mix_without_repeats():
    assert flex.expand("JFK, EWR, new-york-area") == ("JFK", "EWR", "LGA")


def test_an_unknown_place_is_refused_not_guessed():
    with pytest.raises(ValueError, match="I don't know"):
        flex.expand("the moon")


def test_days_beyond_the_limit_are_refused(store, clock):
    with pytest.raises(ValueError, match="0 to"):
        flex.scoped(add_cx(store, clock), days=9, origins=None, destinations=None)


# ---- what gets scouted ------------------------------------------------------------------
def test_scouts_leave_out_what_the_watch_already_covers(store, clock):
    watch = _flexible(store, clock, days=1, origins=("EWR",))
    queries = flex.scout_queries(watch, DAY - timedelta(days=300), lambda d: True)
    assert Query("JFK", "HKG", DAY, 1, "american") not in queries
    assert Query("JFK", "HKG", DAY + timedelta(days=1), 1, "american") in queries
    assert Query("EWR", "HKG", DAY, 1, "american") in queries


def test_scouts_are_capped_and_nearest_first(store, clock):
    watch = _flexible(store, clock, days=3, origins=flex.expand("north-america"))
    queries = flex.scout_queries(watch, DAY - timedelta(days=300), lambda d: True)
    assert len(queries) == flex.MAX_SCOUT_QUERIES
    assert queries[0].date == DAY  # the date asked for comes before neighbouring ones


def test_scouts_skip_dates_the_program_cant_show_yet(store, clock):
    watch = _flexible(store, clock, days=2)
    assert flex.scout_queries(watch, DAY - timedelta(days=300), lambda d: False) == []


def test_a_plain_watch_scouts_nothing(store, clock):
    watch = add_cx(store, clock)
    assert not watch.is_flexible
    assert plan_scouts([watch], DAY, clock(), lambda w, q: None, {"american": 99}, set()) == {}


def test_scouts_only_use_leftover_searches(store, clock):
    watch = _flexible(store, clock, days=3)
    today = DAY - timedelta(days=30)
    none_left = plan_scouts([watch], today, clock(), lambda w, q: None, {"american": 0}, set())
    two_left = plan_scouts([watch], today, clock(), lambda w, q: None, {"american": 2}, set())
    assert none_left == {} and len(two_left) == 2


def test_a_recently_scouted_place_waits(store, clock):
    watch = _flexible(store, clock, days=1)
    today = DAY - timedelta(days=30)
    plan = plan_scouts([watch], today, clock(), lambda w, q: clock(), {"american": 99}, set())
    assert plan == {}


# ---- running them -----------------------------------------------------------------------
def _open(origin="JFK"):
    return answer_from(response(cx_slice(origin=origin, business_miles=80_000)))


def _shut():
    return answer_from(response(cx_slice(business_miles=None)))


async def test_a_nearby_airport_opening_sends_an_urgent_alert_after_a_recheck(store, clock):
    _flexible(store, clock, origins=("EWR",))
    # the watch's own check, then the EWR scout and its live re-check
    runner, fetcher, sender, _ = make_runner(store, clock, [_shut(), _open("EWR"), _open("EWR")])
    await runner.tick()
    assert len(fetcher.queries) == 3
    [subject] = [s for s in sender.subjects if s.startswith("SIMILAR FLIGHT OPEN")]
    assert "EWR-HKG" in subject and "80,000 mi" in subject
    assert "from EWR instead of JFK" in sender.sent[0][1]


async def test_a_scout_that_is_gone_at_the_recheck_sends_nothing(store, clock):
    _flexible(store, clock, origins=("EWR",))
    gone = answer_from(response(cx_slice(origin="EWR", business_miles=None)))
    runner, _, sender, _ = make_runner(store, clock, [_shut(), _open("EWR"), gone])
    await runner.tick()
    assert not [s for s in sender.subjects if s.startswith("SIMILAR")]


async def test_a_scouted_flight_is_announced_once(store, clock):
    _flexible(store, clock, origins=("EWR",))
    answers = [_shut(), _open("EWR"), _open("EWR"), _shut(), _open("EWR")]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now += 7 * 3600  # past the scouting gap, and the watch is due again
    await runner.tick()
    assert sum(s.startswith("SIMILAR FLIGHT OPEN") for s in sender.subjects) == 1


# ---- the command ------------------------------------------------------------------------
def test_the_flex_command_saves_a_scope(store, clock, monkeypatch):
    from jetbridge import service

    watch = add_cx(store, clock)
    monkeypatch.setattr(service, "open_store", lambda: store)
    result = CliRunner().invoke(
        app, ["watch", "flex", str(watch.id), "--days", "1", "--origins", "EWR", "--yes"]
    )
    assert result.exit_code == 0, result.output
    saved = store.get_watch(watch.id)
    assert (saved.flex_days, saved.flex_origins) == (1, ("EWR",))
