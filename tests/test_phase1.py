"""Telling someone early when a watch is wrong, and proving it's being watched."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from conftest import DAY, START, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge import draft, power, service
from jetbridge.insights import coverage
from jetbridge.models import (
    AnswerKind,
    Cabin,
    CheckResult,
    CheckStatus,
    Flight,
    SearchAnswer,
    Watch,
)
from jetbridge.notify.email import flush_outbox
from jetbridge.runner import watch_coverage
from jetbridge.scheduler import group_queries, is_searchable, search_starts, searchable_dates

CLOSED = response(cx_slice(business_miles=None))
HOUR = 3600.0


def watch_on(*days: date, program: str = "american") -> Watch:
    return Watch(1, Flight("CX", "805"), "JFK", "HKG", days, Cabin.BUSINESS, program=program)


# ---- booking windows ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("program", "days_out", "searched"),
    [
        ("american", 334, True),  # three days ahead of the published window, to be safe
        ("american", 335, False),
        ("delta", 334, True),
        ("delta", 335, False),
        ("united", 340, True),
        ("united", 341, False),
        ("american", 0, True),
        ("american", -1, False),
    ],
)
def test_dates_are_only_searched_once_the_program_can_show_them(program, days_out, searched):
    today = date(2027, 1, 10)
    assert is_searchable(today + timedelta(days=days_out), program, today) is searched


def test_booking_window_handles_a_leap_year():
    assert search_starts(date(2028, 3, 1), "american") == date(2028, 3, 1) - timedelta(days=334)


def test_unopened_dates_make_no_requests():
    today = date(2027, 1, 10)
    near, far = today + timedelta(days=100), today + timedelta(days=340)
    watch = watch_on(near, far)
    assert searchable_dates(watch, today) == (near,)
    assert [query.date for query in group_queries([watch], today)] == [near]


async def test_a_date_entering_the_window_is_not_called_blind(store, clock):
    far = datetime.fromtimestamp(clock()).date() + timedelta(days=340)
    add_cx(store, clock, dates=(far,))
    runner, fetcher, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    assert fetcher.queries == []

    clock.now += 6 * 86400  # the date became searchable hours ago, on a watch made days ago
    store.set_next_check(1, clock.now + 6 * HOUR)
    await runner.tick()
    assert fetcher.queries == []
    assert not [s for s in sender.subjects if "can't see" in s]


def test_review_says_when_searching_starts():
    far = date.today() + timedelta(days=340)
    text = draft.review_text(
        draft.WatchDraft("JFK", "HKG", far.isoformat(), "CX805"),
        "owner@x.com",
    )
    assert "about 0 aa.com searches a day" in text
    assert f"starts {search_starts(far, 'american'):%b %d, %Y}" in text


# ---- proof of watching ----------------------------------------------------------------
def test_coverage_on_schedule():
    found = coverage([0, 6 * HOUR, 12 * HOUR, 18 * HOUR], 0, 24 * HOUR, 8 * HOUR)
    assert found.watched_s == 24 * HOUR and found.longest_gap_s == 6 * HOUR


def test_coverage_counts_only_the_normal_gap_after_each_answer():
    found = coverage([0], 0, 24 * HOUR, 8 * HOUR)
    assert found.watched_s == 8 * HOUR and found.longest_gap_s == 24 * HOUR


def test_coverage_with_no_clear_answers():
    found = coverage([], 0, 24 * HOUR, 8 * HOUR)
    assert found.watched_s == 0 and found.longest_gap_s == 24 * HOUR


def test_an_answer_before_the_period_covers_its_start():
    found = coverage([-2 * HOUR], 0, 6 * HOUR, 8 * HOUR)
    assert found.watched_s == 6 * HOUR and found.longest_gap_s == 8 * HOUR


def test_a_period_under_an_hour_means_nothing():
    assert coverage([0], 0, HOUR - 1, 8 * HOUR) is None


def test_coverage_text_reads_plainly():
    text = coverage([0, 6 * HOUR, 12 * HOUR, 18 * HOUR], 0, 24 * HOUR, 8 * HOUR).text
    assert text == "watched 24 of the last 24 hours, longest gap between clear answers 6.0 hours"


async def test_coverage_shows_the_least_watched_date(store, clock):
    day2 = DAY + timedelta(days=1)
    watch = add_cx(store, clock, dates=(DAY, day2), checks_per_day=6)
    good = CheckResult(CheckStatus.NOT_OFFERED)
    state = store.date_state(watch.id, DAY)
    for hours in range(0, 24, 4):
        store.commit_check(watch.id, DAY, good, state, [], clock() + hours * HOUR)
    store.commit_check(watch.id, day2, good, state, [], clock())
    found = watch_coverage(store, watch, clock() + 24 * HOUR)
    assert found.longest_gap_s == 24 * HOUR


def test_unknown_answers_do_not_count_as_watching(store, clock):
    watch = add_cx(store, clock)
    unknown = CheckResult(CheckStatus.UNKNOWN, detail="blocked")
    state = store.date_state(watch.id, DAY)
    store.commit_check(watch.id, DAY, unknown, state, [], clock())
    assert store.clear_answers(watch.id, DAY, clock() - HOUR) == []


async def test_digest_includes_proof_of_watching(store, clock):
    store.set("digest_enabled", "1")
    watch = add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    clock.now = datetime(2027, 1, 11, 7, 0).timestamp()
    await runner.tick()
    clock.now += 2 * HOUR
    store.set_next_check(watch.id, clock.now + HOUR)
    await runner.tick()
    digest = next(body for subject, body in sender.sent if subject.startswith("jetbridge daily"))
    assert "Proof of watching: watched 2 of the last" in digest


# ---- what a first check means ---------------------------------------------------------
@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (CheckResult(CheckStatus.AVAILABLE, miles=110_000), "Seats are open now, at 110,000 miles"),
        (CheckResult(CheckStatus.NOT_OFFERED), "lists this flight"),
        (CheckResult(CheckStatus.NOT_LISTED), "wasn't in its results"),
        (CheckResult(CheckStatus.UNKNOWN, detail="blocked"), "couldn't tell"),
    ],
)
def test_first_check_says_what_it_means(result, expected):
    assert expected in service.first_check_means(watch_on(DAY), result)


def test_unknown_never_reads_as_no_seats():
    text = service.first_check_means(watch_on(DAY), CheckResult(CheckStatus.UNKNOWN, detail="x"))
    assert "no qualifying seats" not in text and "no seats" not in text


# ---- welcome notes --------------------------------------------------------------------
def _pending(store, kind="welcome"):
    return [item for item in store.pending_alerts() if item.kind == kind]


def test_new_people_get_one_note(store, clock):
    watch = add_cx(store, clock, recipients=("ann@x.com", "bo@y.com"))
    assert service.welcome_new_recipients(store, watch, "me@z.com", clock()) == [
        "ann@x.com",
        "bo@y.com",
    ]
    assert {item.recipient for item in _pending(store)} == {"ann@x.com", "bo@y.com"}
    assert service.welcome_new_recipients(store, watch, "me@z.com", clock()) == []
    assert len(_pending(store)) == 2


def test_the_owner_gets_no_note(store, clock):
    watch = add_cx(store, clock, recipients=("me@z.com",))
    assert service.welcome_new_recipients(store, watch, "me@z.com", clock()) == []


def test_each_note_goes_to_one_person_and_names_nobody_else(store, clock):
    watch = add_cx(store, clock, recipients=("ann@x.com", "bo@y.com"))
    service.welcome_new_recipients(store, watch, "me@z.com", clock())
    for item in _pending(store):
        other = "bo@y.com" if item.recipient == "ann@x.com" else "ann@x.com"
        assert other not in item.body and "me@z.com" not in item.body
        assert "CX805" in item.body


def test_someone_added_to_a_second_watch_gets_no_second_note(store, clock):
    first = add_cx(store, clock, recipients=("ann@x.com",))
    second = add_cx(store, clock, recipients=("ann@x.com",))
    service.welcome_new_recipients(store, first, None, clock())
    flush_outbox(store, _Accepts(), clock())
    assert service.welcome_new_recipients(store, second, None, clock()) == []


def test_set_recipients_notes_only_new_people(store, clock, monkeypatch):
    monkeypatch.setattr(service, "_owner_address", lambda: "me@z.com")
    watch = add_cx(store, clock, recipients=("ann@x.com",))
    service.welcome_new_recipients(store, watch, "me@z.com", clock())
    updated, welcomed = service.set_recipients(store, watch, ("ann@x.com", "bo@y.com"))
    assert welcomed == ["bo@y.com"] and updated.recipients == ("ann@x.com", "bo@y.com")


def test_saving_a_watch_queues_the_notes(store, monkeypatch):
    monkeypatch.setattr(service, "_owner_address", lambda: "me@z.com")
    service.create_watch(
        store,
        flight="CX805",
        origin="JFK",
        destination="HKG",
        dates=(date.today() + timedelta(days=30)).isoformat(),
        cabin="business",
        recipients="ann@x.com",
    )
    assert [item.recipient for item in _pending(store)] == ["ann@x.com"]


def test_a_refused_note_tells_the_owner(store, clock):
    from conftest import FakeSender

    watch = add_cx(store, clock, recipients=("bad@x.com",))
    service.welcome_new_recipients(store, watch, None, clock())
    sender = FakeSender()
    sender.refused = {"bad@x.com"}
    flush_outbox(store, sender, clock())
    flush_outbox(store, sender, clock())
    assert any("couldn't email bad@x.com" in subject for subject in sender.subjects)


def test_review_mentions_the_note_only_for_other_people():
    note = "one short note"
    day = (date.today() + timedelta(days=30)).isoformat()
    others = draft.WatchDraft("JFK", "HKG", day, "CX805", recipients="ann@x.com")
    mine = draft.WatchDraft("JFK", "HKG", day, "CX805")
    assert note in draft.review_text(others, "me@z.com")
    assert note not in draft.review_text(mine, "me@z.com")


class _Accepts:
    def send(self, subject, body, to=None):
        pass


# ---- sleep ----------------------------------------------------------------------------
QUERY_ENGLISH = """Power Setting GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da  (Sleep after)
  Possible Settings units: Seconds
Current AC Power Setting Index: 0x00000708
Current DC Power Setting Index: 0x00000384
"""
QUERY_GERMAN = """Energieeinstellungs-GUID: 29f6c1db-86da-48c5-9fdb-f2b67b1f44da
  Einheiten: Sekunden
Index der aktuellen Wechselstromeinstellung: 0x00000000
Index der aktuellen Gleichstromeinstellung: 0x00000384
"""


def test_reads_the_plugged_in_timer_in_any_language():
    assert power.parse_plugged_in(QUERY_ENGLISH) == 1800
    assert power.parse_plugged_in(QUERY_GERMAN) == 0


def test_an_unexpected_answer_is_refused():
    with pytest.raises(ValueError):
        power.parse_plugged_in("nothing useful")


@pytest.fixture
def fake_powercfg(monkeypatch):
    values = {"STANDBYIDLE": 1800, "HIBERNATEIDLE": 7200}
    calls: list[tuple[str, ...]] = []

    def run(*args: str) -> str:
        calls.append(args)
        if args[0] == "/query":
            value = values[args[-1]]
            return f"AC: 0x{value:08x}\nDC: 0x00000384\n"
        name = "STANDBYIDLE" if args[1].startswith("standby") else "HIBERNATEIDLE"
        values[name] = int(args[2]) * 60
        return ""

    monkeypatch.setattr(power, "_powercfg", run)
    return values, calls


def test_keep_awake_and_put_it_back(store, fake_powercfg):
    values, _ = fake_powercfg
    power.keep_awake(store)
    assert values == {"STANDBYIDLE": 0, "HIBERNATEIDLE": 0}
    assert power.restore(store) is True
    assert values == {"STANDBYIDLE": 1800, "HIBERNATEIDLE": 7200}
    assert power.restore(store) is False


def test_turning_it_on_twice_keeps_the_real_original(store, fake_powercfg):
    values, _ = fake_powercfg
    power.keep_awake(store)
    power.keep_awake(store)
    power.restore(store)
    assert values["STANDBYIDLE"] == 1800


def test_nothing_changes_on_a_pc_already_awake(store, fake_powercfg):
    values, calls = fake_powercfg
    values.update(STANDBYIDLE=0, HIBERNATEIDLE=0)
    power.keep_awake(store)
    assert not [c for c in calls if c[0] == "/change"]
    assert power.restore(store) is False


def test_battery_settings_are_never_touched(store, fake_powercfg):
    _, calls = fake_powercfg
    power.keep_awake(store)
    power.restore(store)
    assert all(c[1].endswith("-ac") for c in calls if c[0] == "/change")


def test_a_timer_under_a_minute_is_not_restored_as_never(store, fake_powercfg):
    values, _ = fake_powercfg
    values["STANDBYIDLE"] = 30
    power.keep_awake(store)
    power.restore(store)
    assert values["STANDBYIDLE"] == 60


# ---- an answer can't be confused with a date that wasn't searched ---------------------
async def test_a_blocked_site_is_still_unknown(store, clock):
    add_cx(store, clock)
    blocked = SearchAnswer(AnswerKind.BLOCKED, detail="challenge", retry_at=START + 600)
    runner, _, sender, _ = make_runner(store, clock, [blocked])
    report = await runner.tick()
    assert report.checks[0].result.status is CheckStatus.UNKNOWN
    assert not sender.sent
