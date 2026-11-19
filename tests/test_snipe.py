"""Snipe windows: frequent checks inside a chosen time of day, capped and opt-in."""

from __future__ import annotations

from datetime import datetime

import pytest
from conftest import DAY, add_cx, answer_from, cx_slice, make_runner, response
from typer.testing import CliRunner

from jetbridge import service, snipe
from jetbridge.alerts import DateState
from jetbridge.cli import app
from jetbridge.scheduler import SNIPE, DateView, why_due


def at(hour: int, minute: int = 0) -> float:
    return datetime(2027, 1, 10, hour, minute).timestamp()


# ---- the window itself ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "parsed"),
    [
        ("2:00-5:00", "02:00-05:00"),
        (" 02:30 - 04:00 ", "02:30-04:00"),
        ("22:00-01:00", "22:00-01:00"),
    ],
)
def test_windows_parse_to_one_form(text, parsed):
    assert snipe.parse_window(text) == parsed


def test_auto_means_the_learned_time():
    assert snipe.parse_window("Auto") == snipe.AUTO


@pytest.mark.parametrize(
    "text", ["", "2-5", "25:00-26:00", "02:00-02:00", "00:00-12:00", "02:61-04:00"]
)
def test_unclear_or_too_long_windows_are_refused(text):
    with pytest.raises(ValueError):
        snipe.parse_window(text)


def test_inside_and_outside_a_window_including_across_midnight():
    day = snipe.minutes("08:00-11:00")
    assert snipe.is_inside(day, at(8)) and snipe.is_inside(day, at(10, 59))
    assert not snipe.is_inside(day, at(11)) and not snipe.is_inside(day, at(7, 59))
    night = snipe.minutes("22:00-02:00")
    assert snipe.is_inside(night, at(23)) and snipe.is_inside(night, at(1))
    assert not snipe.is_inside(night, at(12))


def test_the_next_start_is_today_or_tomorrow():
    bounds = snipe.minutes("08:00-11:00")
    assert snipe.next_start(bounds, at(7)) == at(8)
    assert snipe.next_start(bounds, at(9)) == datetime(2027, 1, 11, 8, 0).timestamp()


# ---- through the runner -----------------------------------------------------------------
def _runner_and_watch(store, clock, window="08:00-11:00"):
    shut = answer_from(response(cx_slice(business_miles=None)))
    runner, fetcher, _, _ = make_runner(store, clock, [shut] * 6)
    watch = add_cx(store, clock)
    store.set_snipe(watch.id, window)
    return runner, store.get_watch(watch.id), fetcher


def test_a_date_is_due_inside_the_window_every_gap(store, clock):
    runner, watch, _ = _runner_and_watch(store, clock)  # the clock starts at 9:00
    assert runner._snipe_due(watch, DateState())
    assert runner._snipe_due(watch, DateState(last_check_at=clock() - snipe.SNIPE_GAP_S))
    assert not runner._snipe_due(watch, DateState(last_check_at=clock() - 60))


def test_nothing_is_due_outside_the_window_or_without_one(store, clock):
    runner, watch, _ = _runner_and_watch(store, clock, "14:00-16:00")
    assert not runner._snipe_due(watch, DateState())
    plain = add_cx(store, clock)
    assert not runner._snipe_due(plain, DateState())


def test_the_daily_cap_stops_it(store, clock):
    runner, watch, _ = _runner_and_watch(store, clock)
    for _ in range(snipe.DAILY_CAP):
        store.add_snipe_used(watch.id, runner._today())
    assert not runner._snipe_due(watch, DateState())


def test_auto_waits_for_something_learned(store, clock):
    runner, watch, _ = _runner_and_watch(store, clock, snipe.AUTO)
    assert not runner._snipe_due(watch, DateState())
    store.set("release_pattern:american", '{"start_hour": 8, "count": 5, "total": 6}')
    assert runner._snipe_due(watch, DateState())


def test_wake_up_every_gap_inside_and_at_the_start_outside(store, clock):
    runner, watch, _ = _runner_and_watch(store, clock)
    assert runner._wake_at(watch, clock()) == clock() + snipe.SNIPE_GAP_S
    outside, watch2, _ = _runner_and_watch(store, clock, "14:00-16:00")
    assert outside._wake_at(watch2, clock()) == at(14)


def test_a_snipe_date_is_why_due_ahead_of_ordinary_reasons():
    view = DateView(last_clear_at=1.0, last_check_at=1.0, snipe_due=True)
    watch = add_watch_stub()
    assert why_due(watch, DAY, view, 2.0, DAY.replace(year=2026)) == SNIPE


def add_watch_stub():
    from jetbridge.models import Cabin, Flight, Watch

    return Watch(1, Flight("CX", "805"), "JFK", "HKG", (DAY,), Cabin.BUSINESS)


async def test_a_tick_inside_the_window_checks_again_a_quarter_hour_later(store, clock):
    runner, watch, fetcher = _runner_and_watch(store, clock)
    await runner.tick()
    assert len(fetcher.queries) == 1
    clock.now += 900
    await runner.tick()
    assert len(fetcher.queries) == 2  # normally this date would wait hours
    assert store.snipe_used(watch.id, runner._today()) >= 1


async def test_a_tick_without_a_window_does_not(store, clock):
    runner, _, fetcher = _runner_and_watch(store, clock, None)
    await runner.tick()
    clock.now += 900
    await runner.tick()
    assert len(fetcher.queries) == 1


# ---- saving it --------------------------------------------------------------------------
def test_only_a_few_watches_can_have_one(store, clock):
    watches = [add_cx(store, clock) for _ in range(snipe.MAX_WATCHES + 1)]
    for watch in watches[: snipe.MAX_WATCHES]:
        store.set_snipe(watch.id, "02:00-05:00")
    extra = watches[-1]
    with pytest.raises(ValueError, match="only 3 watches"):
        snipe.check(store, extra, "02:00-05:00")
    assert snipe.check(store, watches[0], "03:00-04:00") == "03:00-04:00"  # editing its own is fine
    assert snipe.check(store, extra, None) is None


def test_the_review_states_the_risk(store, clock):
    watch = add_cx(store, clock)
    text = snipe.review_text(watch, "02:00-05:00")
    assert "most likely thing to get a site to block" in text
    assert "can't promise you'll get a seat" in text


def test_the_command_saves_shows_and_turns_off(store, clock, monkeypatch):
    watch = add_cx(store, clock)
    monkeypatch.setattr(service, "open_store", lambda: store)
    runner = CliRunner()
    saved = runner.invoke(app, ["watch", "snipe", str(watch.id), "--window", "2:00-5:00", "--yes"])
    assert saved.exit_code == 0, saved.output
    assert "most likely thing to get a site to block" in saved.output
    assert store.get_watch(watch.id).snipe_window == "02:00-05:00"
    off = runner.invoke(app, ["watch", "snipe", str(watch.id), "--off"])
    assert off.exit_code == 0 and store.get_watch(watch.id).snipe_window is None
    bad = runner.invoke(app, ["watch", "snipe", str(watch.id), "--window", "nonsense", "--yes"])
    assert bad.exit_code != 0


def test_deleting_a_watch_forgets_its_snipe_count(store, clock):
    watch = add_cx(store, clock)
    store.add_snipe_used(watch.id, DAY)
    store.delete_watch(watch.id)
    assert store.snipe_used(watch.id, DAY) == 0


def test_a_window_across_midnight_spends_one_days_cap():
    night = snipe.minutes("23:00-02:00")
    assert snipe.window_day(night, at(23, 30)) == snipe.window_day(night, at(1, 30)).replace(day=10)
    assert snipe.window_day(night, at(1, 30)).day == 9  # after midnight, still the day it began
    day = snipe.minutes("08:00-11:00")
    assert snipe.window_day(day, at(9)).day == 10


def test_snipe_ranks_below_every_ordinary_reason():
    from jetbridge.scheduler import _SCORES

    assert _SCORES[SNIPE] < min(v for k, v in _SCORES.items() if k != SNIPE)


async def test_only_the_watch_with_a_window_spends_its_cap(store, clock):
    shut = answer_from(response(cx_slice(business_miles=None)))
    runner, _, _, _ = make_runner(store, clock, [shut] * 4)
    sniped, plain = add_cx(store, clock), add_cx(store, clock)  # same route, so one shared search
    store.set_snipe(sniped.id, "08:00-11:00")
    await runner.tick()
    clock.now += 900
    await runner.tick()
    today = runner._today()
    assert store.snipe_used(sniped.id, today) >= 1 and store.snipe_used(plain.id, today) == 0
