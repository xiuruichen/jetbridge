"""Moving searches to where the signals point, without ever going over the limits."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from conftest import DAY, FakeClock, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge.acquire.governor import DAILY_LIMIT, PLANNED_DAILY, Governor
from jetbridge.models import AnswerKind, Cabin, Flight, SearchAnswer, Watch
from jetbridge.scheduler import (
    BURST_GAPS_S,
    CLOSE_IN,
    FIRST_LOOK,
    MOVED,
    SCHEDULE,
    WHY,
    Burst,
    DateView,
    burst_is_due,
    first_night_at,
    first_night_is_due,
    next_visit_at,
    plan_searches,
    spend_burst,
    start_burst,
    watch_checks_per_day,
    why_due,
)

TODAY = date(2027, 1, 10)
NOW = datetime(2027, 1, 10, 12, 0).timestamp()
HOUR = 3600.0
RNG = random.Random(3)
CLOSED = response(cx_slice(business_miles=None))
OPEN = response(cx_slice(seats=2))
OTHER = cx_slice(business_miles=95_000, flight="889")


def out(days: int) -> date:
    return TODAY + timedelta(days=days)


def watch_on(*days: date, id_: int = 1, base: int = 6, high: bool = False, program="american"):
    return Watch(
        id_,
        Flight("CX", "805"),
        "JFK",
        "HKG",
        days,
        Cabin.BUSINESS,
        program=program,
        checks_per_day=base,
        high_priority=high,
    )


def fresh(*_) -> DateView:
    return DateView()


# ---- bursts ---------------------------------------------------------------------------
def test_movement_brings_the_next_check_forward():
    burst = start_burst(None, "seats just went", NOW, random.Random(1))
    assert NOW + BURST_GAPS_S[0] * 0.7 <= burst.next_at <= NOW + BURST_GAPS_S[0] * 1.3
    assert not burst_is_due(burst, NOW)
    assert burst_is_due(burst, burst.next_at)


def test_a_burst_runs_out_after_three_extra_checks():
    burst = start_burst(None, "x", NOW, RNG)
    for expected in (1, 2):
        burst = spend_burst(burst, burst.next_at, RNG)
        assert burst.used == expected and burst.next_at is not None
    burst = spend_burst(burst, burst.next_at, RNG)
    assert burst.used == 3 and burst.next_at is None


def test_more_movement_cant_add_more_checks():
    burst = start_burst(None, "x", NOW, RNG)
    assert start_burst(burst, "y", NOW + 60, RNG) is burst
    for _ in range(3):
        burst = spend_burst(burst, burst.next_at, RNG)
    assert start_burst(burst, "y", NOW + 3600, RNG) is burst


def test_new_movement_can_reuse_what_is_left_of_the_day():
    burst = spend_burst(start_burst(None, "x", NOW, RNG), NOW + HOUR, RNG)
    burst = Burst("x", burst.started_at, burst.used, None)
    again = start_burst(burst, "y", NOW + 2 * HOUR, RNG)
    assert again.used == 1 and again.next_at is not None and again.reason == "y"


def test_a_new_day_starts_a_new_burst():
    burst = Burst("x", NOW - 86401, 3, None)
    again = start_burst(burst, "y", NOW, RNG)
    assert again.used == 0 and again.started_at == NOW


# ---- the first night ------------------------------------------------------------------
def test_first_night_is_midnight_at_the_airlines_home():
    day = date(2027, 12, 1)
    opens = day - timedelta(days=331)
    expected = datetime(opens.year, opens.month, opens.day, tzinfo=ZoneInfo("America/Chicago"))
    assert first_night_at(day, "american") == expected.timestamp() + 300


def test_first_night_uses_each_programs_own_window_and_home():
    day = date(2027, 12, 1)
    assert first_night_at(day, "delta") != first_night_at(day, "american")
    assert first_night_at(day, "united") < first_night_at(day, "american")


def test_first_night_happens_once_and_expires():
    day = date(2027, 12, 1)
    at = first_night_at(day, "american")
    assert not first_night_is_due(day, "american", None, at - 1)
    assert first_night_is_due(day, "american", None, at)
    assert first_night_is_due(day, "american", at - 100, at + 60)
    assert not first_night_is_due(day, "american", at + 30, at + 60)  # already done
    assert not first_night_is_due(day, "american", None, at + 7 * HOUR)  # asleep past it


def test_a_watch_wakes_up_for_a_first_night():
    day = TODAY + timedelta(days=333)
    watch = watch_on(day)
    start = datetime(2027, 1, 10, 12, 0).timestamp()
    assert next_visit_at(watch, start, TODAY, [], RNG) <= first_night_at(day, "american")


# ---- why a date is due ----------------------------------------------------------------
@pytest.mark.parametrize(
    ("view", "expected"),
    [
        (DateView(), FIRST_LOOK),
        (DateView(NOW - 60, NOW - 60), None),
        (DateView(NOW - 60, NOW - 60, Burst("x", NOW - 3600, 0, NOW - 1)), MOVED),
        (DateView(NOW - 60, NOW - 60, Burst("x", NOW - 3600, 0, NOW + 900)), None),
        (DateView(NOW - 86400, NOW - 86400), SCHEDULE),
    ],
)
def test_why_a_far_out_date_with_a_nearer_one_is_due(view, expected):
    watch = watch_on(out(5), out(200))
    assert why_due(watch, out(200), view, NOW, TODAY) == expected


def test_a_close_date_says_so():
    assert why_due(watch_on(out(5)), out(5), DateView(NOW - 60), NOW, TODAY) == CLOSE_IN


@pytest.mark.parametrize("word", ["will", "predict", "likely", "expect", "soon"])
def test_reasons_never_predict(word):
    assert not any(word in text for text in WHY.values())


# ---- the plan -------------------------------------------------------------------------
def test_shared_searches_stay_one_search():
    first, second = watch_on(out(5), id_=1), watch_on(out(5), id_=2)
    plan = plan_searches([first, second], TODAY, NOW, fresh, lambda _: 100)
    assert len(plan) == 1
    assert [w.id for w in next(iter(plan.values())).watches] == [1, 2]


def test_the_most_deserving_goes_first_within_a_site():
    moved = Burst("x", NOW - 3600, 0, NOW - 1)

    def view(watch, day):
        if day == out(200):
            return DateView(NOW - 60, NOW - 60, moved)
        return DateView(NOW - 86400, NOW - 86400)

    plan = plan_searches([watch_on(out(30), out(200))], TODAY, NOW, view, lambda _: 100)
    assert [q.date for q in plan] == [out(200), out(30)]


def test_after_sleep_the_closest_dates_go_first():
    days = (out(40), out(3), out(20))

    def view(watch, day):
        return DateView(NOW - 3 * 86400, NOW - 3 * 86400)

    plan = plan_searches([watch_on(*days)], TODAY, NOW, view, lambda _: 100)
    assert [q.date for q in plan] == [out(3), out(20), out(40)]


def test_a_spent_budget_stops_ordinary_searches():
    def view(watch, day):
        return DateView(NOW - 8 * HOUR, NOW - 8 * HOUR)

    assert plan_searches([watch_on(out(30))], TODAY, NOW, view, lambda _: 0) == {}


def test_a_spent_budget_still_lets_an_overdue_date_through():
    def view(watch, day):
        return DateView(NOW - 13 * HOUR, NOW - 13 * HOUR)

    assert len(plan_searches([watch_on(out(30))], TODAY, NOW, view, lambda _: 0)) == 1


def test_a_spent_budget_still_lets_a_new_date_through():
    assert len(plan_searches([watch_on(out(30))], TODAY, NOW, fresh, lambda _: 0)) == 1


def test_a_burst_cant_spend_past_the_budget():
    def view(watch, day):
        return DateView(NOW - 60, NOW - 60, Burst("x", NOW - 3600, 0, NOW - 1))

    assert plan_searches([watch_on(out(30))], TODAY, NOW, view, lambda _: 0) == {}


def test_budget_counts_requests_not_searches():
    def view(watch, day):
        return DateView(NOW - 8 * HOUR, NOW - 8 * HOUR)

    days = tuple(out(30 + i) for i in range(6))
    plan = plan_searches([watch_on(*days, program="delta")], TODAY, NOW, view, lambda _: 5)
    assert len(plan) == 2  # each delta search counts as 2 requests


def test_sites_have_separate_budgets():
    american, united = watch_on(out(30), id_=1), watch_on(out(30), id_=2, program="united")

    def view(watch, day):
        return DateView(NOW - 8 * HOUR, NOW - 8 * HOUR)

    room = {"american": 0, "united": 100}
    plan = plan_searches([american, united], TODAY, NOW, view, room.__getitem__)
    assert [q.program for q in plan] == ["united"]


def test_a_high_priority_watch_goes_first_when_budget_is_short():
    normal = watch_on(out(30), id_=1)
    important = Watch(
        2,
        Flight("BA", "1"),
        "BOS",
        "LHR",
        (out(30),),
        Cabin.BUSINESS,
        high_priority=True,
    )

    def view(watch, day):
        return DateView(NOW - 8 * HOUR, NOW - 8 * HOUR)

    plan = plan_searches([normal, important], TODAY, NOW, view, lambda _: 1)
    assert [q.origin for q in plan] == ["BOS"]


def test_high_priority_checks_more_often_up_to_the_cap():
    assert watch_checks_per_day(watch_on(out(30), high=True), out(30), TODAY) == 9
    assert watch_checks_per_day(watch_on(out(5), base=24, high=True), out(5), TODAY) == 24


# ---- the budget -----------------------------------------------------------------------
def test_planned_searching_stays_under_the_hard_limit():
    assert PLANNED_DAILY < DAILY_LIMIT


@pytest.mark.parametrize(
    ("hours_since_block", "planned"),
    [
        (1, PLANNED_DAILY // 2),
        (23, PLANNED_DAILY // 2),
        (25, PLANNED_DAILY * 3 // 4),
        (49, PLANNED_DAILY),
    ],
)
def test_a_block_cuts_the_budget_then_it_recovers(store, hours_since_block, planned):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    assert governor.planned_today("delta") == PLANNED_DAILY
    governor.record_block("delta")
    clock.now += hours_since_block * HOUR
    assert governor.planned_today("delta") == planned


def test_a_block_on_one_site_leaves_others_alone(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    governor.record_block("delta")
    assert governor.planned_today("united") == PLANNED_DAILY


def test_room_falls_as_requests_are_used_and_never_goes_negative(store):
    clock = FakeClock()
    governor = Governor(store, clock, clock.sleep)
    for offset in range(PLANNED_DAILY + 5):
        store.record_start("united", clock() - 3600 + offset)
    assert governor.room_today("united") == 0


# ---- through real ticks ---------------------------------------------------------------
async def _run_to_next_visit(runner, store, clock, watch_id=1):
    clock.now = store.get_watch(watch_id).next_check_at + 1
    return await runner.tick()


async def test_seats_going_brings_the_next_check_forward(store, clock):
    add_cx(store, clock)
    answers = [answer_from(OPEN), answer_from(OPEN), answer_from(CLOSED)]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    await _run_to_next_visit(runner, store, clock)
    burst = store.get_burst(1, DAY)
    assert burst.reason == "seats just went"
    assert store.get_watch(1).next_check_at <= burst.next_at
    assert store.get_watch(1).next_check_at - clock() < 1.3 * HOUR + 1


async def test_a_flicker_counts_as_movement(store, clock):
    add_cx(store, clock)
    answers = [answer_from(CLOSED), answer_from(OPEN), answer_from(CLOSED)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    await _run_to_next_visit(runner, store, clock)
    assert not sender.sent
    assert store.get_burst(1, DAY).reason == "seats appeared and went quickly"


async def test_a_price_change_counts_as_movement(store, clock):
    add_cx(store, clock)
    answers = [
        answer_from(OPEN),
        answer_from(OPEN),
        answer_from(response(cx_slice(seats=2, business_miles=90_000))),
    ]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    await _run_to_next_visit(runner, store, clock)
    assert store.get_burst(1, DAY).reason == "the price just changed"


async def test_a_new_flight_on_the_route_counts_as_movement(store, clock):
    add_cx(store, clock)
    answers = [answer_from(CLOSED), answer_from(response(cx_slice(business_miles=None), OTHER))]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    assert store.get_burst(1, DAY) is None
    await _run_to_next_visit(runner, store, clock)
    assert store.get_burst(1, DAY).reason == "another flight opened on this route"


async def test_flights_already_there_on_the_first_look_are_not_movement(store, clock):
    add_cx(store, clock)
    answer = answer_from(response(cx_slice(business_miles=None), OTHER))
    runner, _, _, _ = make_runner(store, clock, [answer])
    await runner.tick()
    assert store.get_burst(1, DAY) is None


async def test_an_opening_moves_neighbouring_dates_up(store, clock):
    near, far = DAY + timedelta(days=2), DAY + timedelta(days=10)
    add_cx(store, clock, dates=(DAY, near, far))
    answers = [answer_from(OPEN), answer_from(OPEN), answer_from(CLOSED), answer_from(CLOSED)]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    assert store.get_burst(1, near).reason == "a neighbouring date opened"
    assert store.get_burst(1, far) is None


@pytest.mark.parametrize(
    "bad",
    [
        SearchAnswer(AnswerKind.ERROR, detail="x"),
        SearchAnswer(AnswerKind.DRIFT, detail="x"),
    ],
)
async def test_an_unclear_answer_is_never_movement(store, clock, bad):
    add_cx(store, clock)
    runner, _, _, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN), bad])
    await runner.tick()
    await _run_to_next_visit(runner, store, clock)
    assert store.get_burst(1, DAY) is None


async def test_a_burst_runs_its_extra_check_then_is_used_up(store, clock):
    add_cx(store, clock)
    answers = [answer_from(OPEN), answer_from(OPEN), answer_from(CLOSED), answer_from(CLOSED)]
    runner, fetcher, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    await _run_to_next_visit(runner, store, clock)
    assert len(fetcher.queries) == 3
    clock.now = store.get_burst(1, DAY).next_at + 1
    store.set_next_check(1, clock.now)
    await runner.tick()
    assert len(fetcher.queries) == 4
    assert store.get_burst(1, DAY).used == 1


async def test_a_resting_site_makes_no_request_for_a_burst(store, clock):
    add_cx(store, clock)
    runner, fetcher, _, _governor = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    store.set_burst(1, DAY, Burst("x", clock(), 0, clock() + 1))
    clock.now += 60
    store.set_next_check(1, clock.now)
    # The fake site answers through the fetcher, so a rest shows up as a deferred answer.
    fetcher.answers = [SearchAnswer(AnswerKind.DEFERRED, detail="resting", retry_at=clock() + 600)]
    report = await runner.tick()
    assert report.stopped_early and "resting" in report.stopped_early
    assert store.get_watch(1).next_check_at >= clock() + 600


async def test_each_observation_records_why_it_ran(store, clock):
    add_cx(store, clock)
    runner, _, _, _ = make_runner(store, clock, [answer_from(CLOSED), answer_from(CLOSED)])
    await runner.tick()
    await runner.tick(only_watch=1)
    assert [row["reason"] for row in store.history(1)] == ["manual", "first_look"]


def test_an_older_database_gains_the_new_columns(tmp_path):
    import sqlite3

    from jetbridge.store import Store

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        "CREATE TABLE observations (id INTEGER PRIMARY KEY, watch_id INT, day TEXT,"
        " observed_at REAL, status TEXT, miles INT, taxes TEXT, currency TEXT, seats TEXT,"
        " detail TEXT);"
        "CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, carrier TEXT, number TEXT,"
        " origin TEXT, destination TEXT, dates TEXT, cabin TEXT, program TEXT, pax INT,"
        " checks_per_day INT, max_miles INT, status TEXT, created_at REAL, next_check_at REAL,"
        " recipients TEXT NOT NULL DEFAULT '[]');"
        "INSERT INTO watches (carrier, number, origin, destination, dates, cabin, program, pax,"
        " checks_per_day, status, created_at, next_check_at)"
        " VALUES ('CX','805','JFK','HKG','[\"2027-05-21\"]','business','american',"
        " 1,6,'active',0,0);"
    )
    old.close()
    store = Store(path)
    assert store.get_watch(1).high_priority is False


def test_deleting_a_watch_deletes_its_bursts(store, clock):
    watch = add_cx(store, clock)
    store.set_burst(watch.id, DAY, Burst("x", clock(), 0, clock() + 60))
    store.delete_watch(watch.id)
    assert store.get_burst(watch.id, DAY) is None


# ---- the worst cases ------------------------------------------------------------------
def _busy_watches(count: int) -> list[Watch]:
    """Separate routes, so nothing is shared and every search costs requests."""
    days = tuple(out(5 + i) for i in range(14))
    return [
        Watch(
            number,
            Flight("CX", "805"),
            f"A{number:02d}",
            "HKG",
            days,
            Cabin.BUSINESS,
            program="united",
            checks_per_day=24,
        )
        for number in range(1, count + 1)
    ]


def _plan_a_day(watches: list[Watch]) -> tuple[int, list[bool]]:
    """Every date moving all day. Returns the requests planned, and for each search made after the
    budget ran out, whether that date was past its minimum pace."""
    used = 0
    after_budget: list[bool] = []
    last: dict[tuple[int, date], float] = {}
    for tick in range(96):
        now = NOW + tick * 900.0

        def view(watch: Watch, day: date, moment: float = now) -> DateView:
            at = last.get((watch.id, day), moment - 3600)
            return DateView(at, at, Burst("x", moment - 60, 0, moment - 1))

        room = max(0, PLANNED_DAILY - used)
        for query, planned in plan_searches(watches, TODAY, now, view, lambda _, r=room: r).items():
            if used >= PLANNED_DAILY:
                overdue = [
                    now - last.get((w.id, query.date), now - 3600) >= 12 * HOUR
                    for w in planned.watches
                ]
                after_budget.append(any(overdue))
            used += 2
            for watch in planned.watches:
                last[(watch.id, query.date)] = now
    return used, after_budget


@pytest.mark.parametrize("watches", [2, 5])
def test_past_the_budget_only_overdue_dates_still_search(watches):
    """Every date moving all day. Ordinary searches and bursts stop at the budget. Dates past their
    minimum pace still go ahead, and the hard limit in the governor stops those if need be."""
    used, after_budget = _plan_a_day(_busy_watches(watches))
    assert after_budget and all(after_budget)
    assert used <= PLANNED_DAILY + 2 * 2 * 14 * watches  # one overdue round past the budget
