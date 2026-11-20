"""American's free calendar: a change on a nearby watched date brings that date's checks forward."""

from __future__ import annotations

from datetime import date, timedelta

from conftest import DAY, add_cx, answer_from, cx_slice, make_runner, response, to_html

from jetbridge.acquire import american
from jetbridge.models import Query

NEXT = DAY + timedelta(days=1)


def with_calendar(days: dict[date, int], *, broken: list[dict] | None = None):
    """A normal answer for DAY, plus the per-day calendar the site sends along."""
    state = response(cx_slice(business_miles=None))
    entries = [
        {"date": d.isoformat(), "awardPointsTotal": str(m), "perPassengerAwardPointsTotal": str(m)}
        for d, m in days.items()
    ]
    state["SearchData"]["weeklyResult"] = {"days": [*entries, *(broken or [])]}
    return answer_from(state)


def test_the_real_fixture_has_a_thirteen_day_calendar(aa_state):
    parsed = american.parse(to_html(aa_state))
    assert len(parsed.calendar) == 13
    assert parsed.calendar[0] == (date(2027, 5, 22), 77_000)


def test_no_calendar_is_fine_and_odd_entries_are_skipped():
    assert answer_from(response(cx_slice())).parsed.calendar == ()
    odd = [{"date": "not a date", "awardPointsTotal": "1"}, {"date": "2027-05-22"}, "junk"]
    parsed = with_calendar({NEXT: 50_000}, broken=odd).parsed
    assert parsed.calendar == ((NEXT, 50_000),) and parsed.slices


def test_a_bad_calendar_never_makes_a_good_answer_unreadable():
    state = response(cx_slice())
    state["SearchData"]["weeklyResult"] = {"days": "nonsense"}
    assert answer_from(state).parsed.slices


# ---- the store --------------------------------------------------------------------------
def day_map(**by_day: int) -> dict[str, int]:
    return {d: m for d, m in by_day.items()}


def test_the_first_calendar_is_a_baseline(store):
    assert store.record_calendar("american", "JFK", "HKG", {"2027-05-22": 50_000}, 0.0) == []


def test_only_days_in_both_calendars_and_that_changed_count(store):
    store.record_calendar("american", "JFK", "HKG", {"2027-05-21": 1, "2027-05-22": 2}, 0.0)
    moved = store.record_calendar(
        "american", "JFK", "HKG", {"2027-05-22": 9, "2027-05-23": 3, "2027-05-21": 1}, 60.0
    )
    assert moved == [date(2027, 5, 22)]  # 05-23 is new because the window slid, not a change
    kinds = [r["kind"] for r in store.conn.execute("SELECT kind FROM offer_events")]
    assert kinds == ["calendar"]
    assert store.release_events("american", 10**9) == []  # a calendar move is not a release


# ---- through the runner -----------------------------------------------------------------
def moved_after(store, clock, *, watched=(DAY, NEXT), program_watch="american"):
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock, dates=watched, program=program_watch)
    query = Query("JFK", "HKG", DAY, 1, "american")
    runner._record(query, with_calendar({DAY: 70_000, NEXT: 70_000}))
    runner._record(query, with_calendar({DAY: 65_000, NEXT: 50_000}))
    return watch


def test_a_nearby_watched_date_is_checked_sooner_when_its_calendar_moves(store, clock):
    watch = moved_after(store, clock)
    burst = store.get_burst(watch.id, NEXT)
    assert burst is not None and "calendar" in burst.reason
    assert store.get_burst(watch.id, DAY) is None  # the searched date is being checked anyway


def test_a_date_nobody_watches_is_left_alone(store, clock):
    watch = moved_after(store, clock, watched=(DAY,))
    assert store.get_burst(watch.id, NEXT) is None


def test_another_programs_watch_is_left_alone(store, clock):
    watch = moved_after(store, clock, program_watch="delta")
    assert store.get_burst(watch.id, NEXT) is None


def test_an_unchanged_calendar_moves_nothing(store, clock):
    runner, *_ = make_runner(store, clock, [])
    watch = add_cx(store, clock, dates=(DAY, NEXT))
    query = Query("JFK", "HKG", DAY, 1, "american")
    for _ in range(2):
        runner._record(query, with_calendar({DAY: 70_000, NEXT: 70_000}))
    assert store.get_burst(watch.id, NEXT) is None
