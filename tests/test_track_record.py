"""A flight's own record: past openings, near misses, other flights, and edits that keep it."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from conftest import (
    DAY,
    add_cx,
    answer_from,
    cx_slice,
    make_runner,
    response,
)
from typer.testing import CliRunner

from jetbridge import service
from jetbridge.cli import app
from jetbridge.insights import Seen, alert_note, clear_answers, openings
from jetbridge.matcher import also_seen, evaluate
from jetbridge.models import AnswerKind, CheckStatus, Query, SearchAnswer
from jetbridge.runner import above_limit_miles

HOUR = 3600.0
OPEN = "available"
SHUT = "not_offered"
QUERY = Query("JFK", "HKG", DAY)


# ---- openings -------------------------------------------------------------------------
def test_an_opening_gives_a_range_because_checks_are_hours_apart():
    history = [
        Seen(0, SHUT),
        Seen(2 * HOUR, OPEN, 110_000),
        Seen(4 * HOUR, OPEN, 120_000),
        Seen(8 * HOUR, SHUT),
    ]
    [found] = openings(history)
    assert found.lasted(9 * HOUR) == "open for between 2 hours and 8 hours"
    assert (found.lowest_miles, found.highest_miles) == (110_000, 120_000)


def test_unknown_neither_ends_nor_starts_an_opening():
    history = [
        Seen(0, SHUT),
        Seen(HOUR, "unknown"),
        Seen(2 * HOUR, OPEN),
        Seen(3 * HOUR, "unknown"),
        Seen(4 * HOUR, OPEN),
        Seen(5 * HOUR, SHUT),
    ]
    assert len(openings(history)) == 1
    assert openings([Seen(0, "unknown")]) == []


def test_two_separate_openings():
    history = [Seen(0, OPEN), Seen(HOUR, SHUT), Seen(2 * HOUR, OPEN), Seen(3 * HOUR, SHUT)]
    assert len(openings(history)) == 2


def test_a_still_open_date_says_so():
    [found] = openings([Seen(0, SHUT), Seen(HOUR, OPEN)])
    assert found.closed_seen is None
    assert found.lasted(5 * HOUR) == "open for at least 4 hours so far"


def test_seats_gone_by_the_recheck_read_as_brief():
    [found] = openings([Seen(0, SHUT), Seen(HOUR, OPEN), Seen(HOUR + 90, SHUT)])
    assert found.lasted(9 * HOUR) == "gone within 2 minutes of being seen"


def test_an_opening_from_the_very_first_answer_has_no_upper_limit():
    [found] = openings([Seen(0, OPEN), Seen(2 * HOUR, OPEN), Seen(4 * HOUR, SHUT)])
    assert found.lasted(9 * HOUR) == "open for at least 2 hours"


def test_order_of_rows_does_not_matter():
    history = [Seen(2 * HOUR, SHUT), Seen(0, SHUT), Seen(HOUR, OPEN)]
    [found] = openings(history)
    assert found.clear_before == 0 and found.closed_seen == 2 * HOUR


def test_clear_answers_leave_out_unknown():
    assert clear_answers([Seen(0, SHUT), Seen(1, "unknown"), Seen(2, OPEN)]) == 2


# ---- the line in an alert -------------------------------------------------------------
def test_alert_note_counts_only_past_openings():
    history = [Seen(0, SHUT), Seen(HOUR, OPEN)]  # the one being announced right now
    assert alert_note(history, 2 * HOUR) is None


def test_alert_note_says_little_from_little():
    assert alert_note([Seen(i, SHUT) for i in range(5)], 10) is None


def test_alert_note_for_a_first_opening_after_many_answers():
    assert "first seats" in alert_note([Seen(i, SHUT) for i in range(6)], 10)


def test_alert_note_describes_the_latest_past_opening():
    history = [
        Seen(0, SHUT),
        Seen(HOUR, OPEN),
        Seen(3 * HOUR, OPEN),
        Seen(5 * HOUR, SHUT),
        Seen(6 * HOUR, OPEN),
    ]
    note = alert_note(history, 7 * HOUR)
    assert (
        note
        == "Seen open once before on this date, most recently open for between 2 hours and 5 hours."
    )


@pytest.mark.parametrize("word", ["usually", "likely", "expect", "will open", "predict"])
def test_alert_note_never_predicts(word):
    history = [Seen(0, SHUT), Seen(HOUR, OPEN), Seen(2 * HOUR, SHUT), Seen(3 * HOUR, OPEN)]
    assert word not in alert_note(history, 4 * HOUR)


async def test_a_real_alert_carries_the_history_line(store, clock):
    closed = answer_from(response(cx_slice(business_miles=None)))
    opened = answer_from(response(cx_slice(seats=2)))
    add_cx(store, clock)
    answers = [closed, opened, opened, closed, opened, opened]
    runner, _, sender, _ = make_runner(store, clock, answers)
    for _ in range(4):
        await runner.tick()
        clock.now = store.get_watch(1).next_check_at + 1
    alerts = [body for subject, body in sender.sent if subject.startswith("SEATS OPEN")]
    assert len(alerts) == 2
    assert "History:" not in alerts[0]
    assert "History:  Seen open once before" in alerts[1]


# ---- near misses ----------------------------------------------------------------------
async def test_seats_over_the_limit_are_recorded_but_not_open(store, clock):
    watch = add_cx(store, clock, max_miles=100_000)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response(cx_slice()))])
    report = await runner.tick()
    result = report.checks[0].result
    assert result.status is CheckStatus.NOT_OFFERED and result.miles == 110_000
    state = store.date_state(watch.id, DAY)
    assert state.status is CheckStatus.NOT_OFFERED and state.miles is None
    assert not sender.sent
    assert above_limit_miles(store, watch, DAY) == 110_000


async def test_near_miss_clears_when_seats_are_gone(store, clock):
    watch = add_cx(store, clock, max_miles=100_000)
    answers = [
        answer_from(response(cx_slice())),
        answer_from(response(cx_slice(business_miles=None))),
    ]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert above_limit_miles(store, watch, DAY) is None


async def test_an_unknown_answer_leaves_the_last_near_miss(store, clock):
    watch = add_cx(store, clock, max_miles=100_000)
    answers = [answer_from(response(cx_slice())), SearchAnswer(AnswerKind.ERROR, detail="x")]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert above_limit_miles(store, watch, DAY) == 110_000


async def test_digest_shows_a_near_miss(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock, max_miles=100_000)
    clock.now = datetime(2027, 1, 11, 9, 0).timestamp()
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response(cx_slice()))])
    await runner.tick()
    digest = next(body for subject, body in sender.sent if subject.startswith("jetbridge daily"))
    assert "seats at 110,000 miles, above your 100,000 limit" in digest


# ---- other flights in the same answer -------------------------------------------------
def _other(miles: int | None = 95_000, flight: str = "889") -> dict:
    return cx_slice(business_miles=miles, flight=flight)


def test_other_flights_with_seats_are_found(store, clock):
    watch = add_cx(store, clock)
    answer = answer_from(response(cx_slice(business_miles=None), _other()))
    assert evaluate(watch, DAY, answer).status is CheckStatus.NOT_OFFERED
    [seen] = also_seen(watch, QUERY, answer)
    assert (seen.flight, seen.miles) == ("CX889", 95_000)


def test_the_watched_flight_is_not_its_own_other_flight(store, clock):
    watch = add_cx(store, clock)
    assert also_seen(watch, QUERY, answer_from(response(cx_slice()))) == []


def test_other_flights_without_seats_are_left_out(store, clock):
    watch = add_cx(store, clock)
    answer = answer_from(response(cx_slice(business_miles=None), _other(None)))
    assert also_seen(watch, QUERY, answer) == []


def test_other_flights_follow_the_watch_price_limit(store, clock):
    watch = add_cx(store, clock, max_miles=90_000)
    answer = answer_from(response(cx_slice(business_miles=None), _other(95_000)))
    assert also_seen(watch, QUERY, answer) == []


@pytest.mark.parametrize(
    "answer",
    [
        SearchAnswer(AnswerKind.BLOCKED, detail="x"),
        SearchAnswer(AnswerKind.DRIFT, detail="x"),
        answer_from(response(_other(), error="309")),
    ],
)
def test_a_bad_answer_shows_no_other_flights(store, clock, answer):
    assert also_seen(add_cx(store, clock), QUERY, answer) == []


async def test_other_flights_cost_no_extra_searches_or_alerts(store, clock):
    watch = add_cx(store, clock)
    answer = answer_from(response(cx_slice(business_miles=None), _other()))
    runner, fetcher, sender, _ = make_runner(store, clock, [answer])
    await runner.tick()
    assert len(fetcher.queries) == 1 and not sender.sent
    saved = store.get_also_seen(watch.id, DAY)
    assert saved["flights"][0]["flight"] == "CX889"


async def test_digest_mentions_other_flights_plainly(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock)
    clock.now = datetime(2027, 1, 11, 9, 0).timestamp()
    answer = answer_from(response(cx_slice(business_miles=None), _other()))
    runner, _, sender, _ = make_runner(store, clock, [answer])
    await runner.tick()
    digest = next(body for subject, body in sender.sent if subject.startswith("jetbridge daily"))
    assert "Also seen with business seats" in digest and "CX889 (95,000 miles)" in digest
    assert "isn't watching these" in digest


async def test_a_later_answer_without_them_clears_other_flights(store, clock):
    watch = add_cx(store, clock)
    answers = [
        answer_from(response(cx_slice(business_miles=None), _other())),
        answer_from(response(cx_slice(business_miles=None))),
    ]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert store.get_also_seen(watch.id, DAY) is None


async def test_an_unclear_answer_keeps_what_was_last_seen(store, clock):
    watch = add_cx(store, clock)
    answers = [
        answer_from(response(cx_slice(business_miles=None), _other())),
        SearchAnswer(AnswerKind.ERROR, detail="x"),
    ]
    runner, _, _, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert store.get_also_seen(watch.id, DAY) is not None


def test_deleting_a_watch_deletes_what_it_saw(store, clock):
    watch = add_cx(store, clock)
    other = add_cx(store, clock)
    flights = [{"flight": "CX889", "departs": "x", "miles": 1}]
    store.set_also_seen(watch.id, DAY, flights, clock())
    store.set_also_seen(other.id, DAY, flights, clock())
    store.delete_watch(watch.id)
    assert store.get_also_seen(watch.id, DAY) is None
    assert store.get_also_seen(other.id, DAY) is not None


# ---- editing keeps the history --------------------------------------------------------
FUTURE = date.today() + timedelta(days=100)
LATER = FUTURE + timedelta(days=1)


def _with_history(store, clock):
    from jetbridge.models import CheckResult

    watch = add_cx(store, clock, dates=(FUTURE,))
    state = store.date_state(watch.id, FUTURE)
    store.commit_check(watch.id, FUTURE, CheckResult(CheckStatus.NOT_OFFERED), state, [], clock())
    return watch


def _change(store, watch, **kwargs):
    fields = {
        "dates": FUTURE.isoformat(),
        "checks_per_day": watch.checks_per_day,
        "max_miles": watch.max_miles,
    } | kwargs
    return service.change_watch(store, watch, **fields)


def test_changing_dates_keeps_the_history(store, clock):
    watch = _with_history(store, clock)
    updated = _change(store, watch, dates=f"{FUTURE},{LATER}")
    assert updated.dates == (FUTURE, LATER)
    assert len(store.history(watch.id)) == 1


def test_new_dates_are_checked_on_the_next_run(store, clock):
    watch = _with_history(store, clock)
    store.set_next_check(watch.id, clock() + 10**6)
    updated = _change(store, watch, dates=f"{FUTURE},{LATER}", now=clock())
    assert updated.next_check_at == clock()


def test_a_new_limit_alone_leaves_the_schedule(store, clock):
    watch = _with_history(store, clock)
    store.set_next_check(watch.id, clock() + 10**6)
    updated = _change(store, watch, max_miles=90_000)
    assert updated.max_miles == 90_000 and updated.next_check_at == clock() + 10**6


def test_the_limit_can_be_removed(store, clock):
    watch = add_cx(store, clock, dates=(FUTURE,), max_miles=90_000)
    assert _change(store, watch, max_miles=None).max_miles is None


def test_a_finished_watch_given_new_dates_starts_again(store, clock):
    watch = _with_history(store, clock)
    store.set_watch_status(watch.id, "finished")
    watch = store.get_watch(watch.id)
    assert _change(store, watch, dates=LATER.isoformat()).status == "active"


def test_a_paused_watch_stays_paused(store, clock):
    watch = _with_history(store, clock)
    store.set_watch_status(watch.id, "paused")
    watch = store.get_watch(watch.id)
    assert _change(store, watch, dates=LATER.isoformat()).status == "paused"


def test_a_bad_change_changes_nothing(store, clock):
    watch = _with_history(store, clock)
    with pytest.raises(ValueError):
        _change(store, watch, checks_per_day=99)
    assert store.get_watch(watch.id).dates == (FUTURE,)


def test_dates_all_in_the_past_are_refused(store, clock):
    watch = _with_history(store, clock)
    with pytest.raises(ValueError, match="passed"):
        _change(store, watch, dates="2020-01-01")


# ---- the command ----------------------------------------------------------------------
@pytest.fixture
def cli_home(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    store = service.open_store()
    watch = store.add_watch(*_flight_args(), now=0.0)
    return store, watch


def _flight_args():
    from jetbridge.models import Cabin, Flight

    return Flight("CX", "805"), "JFK", "HKG", (FUTURE,), Cabin.BUSINESS


def test_edit_asks_first_and_saves_on_yes(cli_home):
    store, watch = cli_home
    runner = CliRunner()
    declined = runner.invoke(app, ["watch", "edit", "1", "--max-miles", "90000"], input="n\n")
    assert "history is kept" in declined.output and "nothing was changed" in declined.output
    assert store.get_watch(watch.id).max_miles is None
    runner.invoke(app, ["watch", "edit", "1", "--max-miles", "90000"], input="y\n")
    assert store.get_watch(watch.id).max_miles == 90_000


def test_edit_refuses_a_limit_and_no_limit_together(cli_home):
    result = CliRunner().invoke(
        app, ["watch", "edit", "1", "--max-miles", "90000", "--no-limit", "--yes"]
    )
    assert result.exit_code != 0


def test_insights_command_reads_plainly(cli_home):
    from jetbridge.models import CheckResult

    store, watch = cli_home
    state = store.date_state(watch.id, FUTURE)
    now = datetime.now().timestamp()
    store.commit_check(
        watch.id, FUTURE, CheckResult(CheckStatus.NOT_OFFERED), state, [], now - HOUR
    )
    opened = CheckResult(CheckStatus.AVAILABLE, miles=110_000, seats_at_least=1)
    store.commit_check(watch.id, FUTURE, opened, state, [], now - 60)
    output = CliRunner().invoke(app, ["insights", "1"]).output
    assert "2 clear answers" in output and "110,000 miles" in output
    assert "doesn't predict" in output
