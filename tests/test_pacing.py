"""Spending searches where they matter, and never more than a site's daily limit."""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from conftest import START, FakeClock, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge import draft
from jetbridge.acquire.governor import DAILY_LIMIT, Governor, GovernorDeferred
from jetbridge.models import Cabin, CheckStatus, Flight, Watch
from jetbridge.scheduler import (
    checks_per_day_on,
    date_is_due,
    fastest_checks_per_day,
    normal_gap_s,
    searches_per_day,
)

TODAY = date(2027, 1, 10)
HOUR = 3600.0
CLOSED = response(cx_slice(business_miles=None))


def out(days: int) -> date:
    return TODAY + timedelta(days=days)


def watch_on(*days: date, base: int = 6, program: str = "american") -> Watch:
    return Watch(
        1,
        Flight("CX", "805"),
        "JFK",
        "HKG",
        days,
        Cabin.BUSINESS,
        program=program,
        checks_per_day=base,
    )


@pytest.mark.parametrize(
    ("days_out", "base", "rate"),
    [
        (0, 6, 12),  # departure day
        (14, 6, 12),
        (15, 6, 6),
        (60, 6, 6),
        (61, 6, 3),  # far out is half
        (200, 6, 3),
        (200, 3, 2),  # half would be 1.5, but 2 is the floor
        (200, 2, 2),
        (200, 1, 1),  # never faster than someone asked for
        (14, 1, 2),
        (14, 12, 24),
        (14, 24, 24),  # capped
        (327, 6, 6 / 2),  # not yet in the days just after opening
        (328, 6, 12),  # American's window opened within the last 3 days
        (331, 6, 12),
        (334, 6, 12),  # the 3 early days, before it should open
    ],
)
def test_pace_by_days_before_departure(days_out, base, rate):
    assert checks_per_day_on(out(days_out), "american", base, TODAY) == rate


def test_the_opening_rush_follows_each_program():
    assert checks_per_day_on(out(330), "united", 6, TODAY) == 3
    assert checks_per_day_on(out(334), "united", 6, TODAY) == 12


def test_a_watch_keeps_pace_with_its_busiest_date():
    watch = watch_on(out(5), out(200))
    assert fastest_checks_per_day(watch, TODAY) == 12


def test_a_watch_with_nothing_searchable_keeps_its_own_setting():
    assert fastest_checks_per_day(watch_on(out(340)), TODAY) == 6


def test_slow_dates_wait_while_the_busiest_is_always_due():
    near, far = out(5), out(200)
    watch = watch_on(near, far)
    start = 1000.0
    assert date_is_due(watch, near, start, start + 1, TODAY)
    assert not date_is_due(watch, far, start, start + 2 * HOUR, TODAY)
    assert date_is_due(watch, far, start, start + 8 * HOUR, TODAY)


def test_a_date_without_a_clear_answer_stays_due():
    watch = watch_on(out(5), out(200))
    assert date_is_due(watch, out(200), None, 1000.0, TODAY)


def test_normal_gap_allows_for_waiting_on_the_next_visit():
    watch = watch_on(out(5), out(200))
    assert normal_gap_s(watch, out(5), TODAY) == 2 * HOUR * 1.3
    assert normal_gap_s(watch, out(200), TODAY) == pytest.approx(8 * HOUR * 0.9 + 2 * HOUR * 1.3)


def test_search_counts_now_and_at_the_busiest():
    days = (out(200), out(201), out(202))
    assert searches_per_day(days, "american", 6, TODAY) == (9, 36)


def test_search_counts_leave_out_passed_and_unopened_dates():
    assert searches_per_day((out(-1), out(20)), "american", 6, TODAY) == (6, 12)
    assert searches_per_day((out(340),), "american", 6, TODAY) == (0, 12)


# ---- through a real tick --------------------------------------------------------------
async def test_a_tick_only_searches_the_dates_that_are_due(store, clock):
    today = datetime.fromtimestamp(clock()).date()
    near, far = today + timedelta(days=5), today + timedelta(days=200)
    watch = add_cx(store, clock, dates=(near, far))
    answers = [answer_from(response())] * 3
    runner, fetcher, _, _ = make_runner(store, clock, answers)

    await runner.tick()
    assert [q.date for q in fetcher.queries] == [near, far]  # both new

    clock.now += 2 * HOUR
    store.set_next_check(watch.id, clock.now)
    await runner.tick()
    assert [q.date for q in fetcher.queries] == [near, far, near]  # the far one isn't due


async def test_a_manual_check_looks_at_every_searchable_date(store, clock):
    today = datetime.fromtimestamp(clock()).date()
    near, far = today + timedelta(days=5), today + timedelta(days=200)
    add_cx(store, clock, dates=(near, far))
    answers = [answer_from(response())] * 4
    runner, fetcher, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now += 2 * HOUR
    await runner.tick(only_watch=1)
    assert [q.date for q in fetcher.queries] == [near, far, near, far]


async def test_next_check_follows_the_busiest_date(store, clock):
    today = datetime.fromtimestamp(clock()).date()
    add_cx(store, clock, dates=(today + timedelta(days=5),))
    runner, _, _, _ = make_runner(store, clock, [answer_from(response())])
    start = clock()
    await runner.tick()
    gap = store.get_watch(1).next_check_at - start
    assert 2 * HOUR * 0.7 <= gap <= 2 * HOUR * 1.3 + 1


async def test_pacing_does_not_call_a_far_out_watch_blind(store, clock):
    today = datetime.fromtimestamp(clock()).date()
    add_cx(store, clock, dates=(today + timedelta(days=200),), checks_per_day=2)
    answers = [answer_from(CLOSED)] * 4
    runner, _, sender, _ = make_runner(store, clock, answers)
    for _ in range(4):
        await runner.tick()
        clock.now = store.get_watch(1).next_check_at
    assert not [s for s in sender.subjects if "can't see" in s]


async def test_an_unclear_date_is_not_held_back_by_pacing(store, clock):
    from jetbridge.models import AnswerKind, SearchAnswer

    today = datetime.fromtimestamp(clock()).date()
    near, far = today + timedelta(days=5), today + timedelta(days=200)
    add_cx(store, clock, dates=(near, far))
    error = SearchAnswer(AnswerKind.ERROR, detail="network")
    runner, fetcher, _, _ = make_runner(store, clock, [error, error, error, error])
    await runner.tick()
    clock.now += 2 * HOUR
    store.set_next_check(1, clock.now)
    report = await runner.tick()
    assert [q.date for q in fetcher.queries] == [near, far, near, far]
    assert all(line.result.status is CheckStatus.UNKNOWN for line in report.checks)


# ---- the daily limit ------------------------------------------------------------------
def _fill(store, site: str, count: int, at: float) -> None:
    for offset in range(count):
        store.record_start(site, at + offset)


async def test_the_daily_limit_defers_instead_of_searching(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    _fill(store, "united", DAILY_LIMIT, clock() - 20 * HOUR)
    with pytest.raises(GovernorDeferred) as caught:
        await governor.acquire("united", clock() + 1e9)  # a long deadline still doesn't wait
    assert "daily" in caught.value.why
    assert caught.value.retry_at == pytest.approx(clock() - 20 * HOUR + 86400 + 0.05)


async def test_one_site_at_its_limit_leaves_others_alone(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    _fill(store, "united", DAILY_LIMIT, clock() - 20 * HOUR)
    await governor.acquire("delta", clock() + 60)
    assert governor.used_today("delta") == 1


async def test_the_daily_limit_clears_after_a_day(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    _fill(store, "united", DAILY_LIMIT, clock() - 25 * HOUR)
    await governor.acquire("united", clock() + 60)
    assert governor.used_today("united") == 1


def test_requests_just_under_the_limit_still_go(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    _fill(store, "united", DAILY_LIMIT - 1, clock() - 20 * HOUR)
    asyncio.run(governor.acquire("united", clock() + 60))


async def test_hitting_the_limit_is_unknown_never_no_seats(store, clock):
    from jetbridge.acquire.fetcher import LiveFetcher

    assert clock() == START
    governor = Governor(store, clock, clock.sleep)
    _fill(store, "american", DAILY_LIMIT, clock() - 20 * HOUR)

    class NoBrowser:
        def is_open(self, site):
            return False

        async def page(self, *_, **__):
            raise AssertionError("searched past the daily limit")

        async def close(self):
            pass

    from jetbridge.models import AnswerKind, Query

    fetcher = LiveFetcher(governor, NoBrowser())
    day = datetime.fromtimestamp(clock()).date() + timedelta(days=30)
    answer = await fetcher.search(Query("JFK", "HKG", day), clock() + 60)
    assert answer.kind is AnswerKind.DEFERRED


# ---- the review -----------------------------------------------------------------------
def _review(days_out: int, count: int = 3, base: int = 6) -> str:
    start = date.today() + timedelta(days=days_out)
    end = start + timedelta(days=count - 1)
    dates = f"{start.isoformat()}..{end.isoformat()}"
    return draft.review_text(
        draft.WatchDraft("JFK", "HKG", dates, "CX805", checks_per_day=base), "me@x.com"
    )


def test_review_shows_searches_now_and_at_the_busiest():
    assert "about 9 aa.com searches a day, up to 36 a day close to departure" in _review(200)


def test_review_warns_when_the_busiest_is_a_lot():
    assert "block more likely" in _review(200, count=4)
    assert "block more likely" not in _review(200, count=3)


def test_review_warns_about_going_over_the_daily_limit():
    text = _review(200, count=14)
    assert f"limit of {DAILY_LIMIT} requests a day" in text
    assert "block more likely" not in text
