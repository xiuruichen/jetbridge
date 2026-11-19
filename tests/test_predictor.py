"""Learning a time of day from a computer's own openings, and using it for one extra check a day."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from conftest import DAY, add_cx, make_runner

from jetbridge.alerts import DateState
from jetbridge.insights import Seen
from jetbridge.predictor import MIN_OPENINGS, Pattern, find_pattern, first_seen_times
from jetbridge.scheduler import PATTERN, DateView, why_due

HOUR = 3600.0


def at(day: int, hour: int) -> float:
    return datetime(2027, 1, day, hour, 30).timestamp()


def test_too_few_openings_say_nothing():
    assert find_pattern([at(d, 2) for d in range(1, MIN_OPENINGS)]) is None


def test_openings_spread_over_the_day_say_nothing():
    assert (
        find_pattern(
            [at(d, h) for d, h in zip(range(1, 9), (1, 4, 7, 10, 13, 16, 19, 22), strict=True)]
        )
        is None
    )


def test_openings_clustered_in_a_few_hours_give_a_window():
    found = find_pattern([at(d, 2) for d in range(1, 6)] + [at(6, 3), at(7, 15)])
    assert found is not None and found.total == 7 and found.count == 6
    assert found.text.startswith("6 of 7 past openings were first seen between")


def test_a_window_can_wrap_midnight():
    found = find_pattern([at(d, 23) for d in range(1, 4)] + [at(4, 0), at(5, 1)])
    assert found is not None and found.start_hour == 23 and found.count == 5


def test_only_openings_with_a_tight_record_count():
    tight = [Seen(0, "not_offered"), Seen(HOUR, "available"), Seen(2 * HOUR, "not_offered")]
    loose = [Seen(0, "not_offered"), Seen(20 * HOUR, "available")]  # started somewhere in 20 hours
    first_open = [Seen(HOUR, "available")]  # no answer before it at all
    assert first_seen_times([tight, loose, first_open]) == [HOUR]


def test_window_timing():
    pattern = Pattern(start_hour=8, count=5, total=6)
    now = datetime(2027, 1, 10, 9, 0).timestamp()
    assert pattern.is_inside(now)
    assert pattern.window_start_today(now) == datetime(2027, 1, 10, 8, 0).timestamp()
    assert pattern.next_start(now) == datetime(2027, 1, 11, 8, 0).timestamp()
    assert not pattern.is_inside(datetime(2027, 1, 10, 12, 0).timestamp())


def test_a_date_inside_the_window_is_due_for_its_pattern_check():
    view = DateView(last_clear_at=1.0, last_check_at=1.0, pattern_due=True)
    assert why_due(add_watch_stub(), DAY, view, 2.0, DAY - timedelta(days=100)) == PATTERN


def add_watch_stub():
    from jetbridge.models import Cabin, Flight, Watch

    return Watch(1, Flight("CX", "805"), "JFK", "HKG", (DAY,), Cabin.BUSINESS)


def _set_pattern(store, start_hour):
    store.set(
        "release_pattern:american", json.dumps({"start_hour": start_hour, "count": 5, "total": 6})
    )


def test_one_extra_check_a_day_inside_the_window(store, clock):
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock)
    _set_pattern(store, 8)  # the clock starts at 9:00, inside 8-11
    assert runner._pattern_due(watch, DateState())
    morning = datetime(2027, 1, 10, 8, 30).timestamp()
    assert not runner._pattern_due(watch, DateState(last_check_at=morning))
    assert runner._pattern_due(watch, DateState(last_check_at=morning - 86400))


def test_outside_the_window_or_without_a_pattern_nothing_changes(store, clock):
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock)
    assert not runner._pattern_due(watch, DateState())
    _set_pattern(store, 14)
    assert not runner._pattern_due(watch, DateState())


async def test_a_tick_learns_the_pattern_once_a_day(store, clock, monkeypatch):
    runner, *_ = make_runner(store, clock, [])
    times = [at(d, 2) for d in range(1, 8)]
    monkeypatch.setattr("jetbridge.runner.first_seen_times", lambda histories: times)
    runner._learn_patterns()
    saved = json.loads(store.get("release_pattern:american"))
    assert saved["start_hour"] in (0, 1, 2) and saved["count"] == 7
    monkeypatch.setattr("jetbridge.runner.first_seen_times", lambda histories: [])
    runner._learn_patterns()  # already done today, so it keeps what it has
    assert store.get("release_pattern:american") is not None
    clock.now += 86400
    runner._learn_patterns()
    assert store.get("release_pattern:american") is None
