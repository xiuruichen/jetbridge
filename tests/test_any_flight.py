"""Watching any nonstop on a route, several at once, and the reminder that takes no searches."""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from conftest import DAY, FakeSender, answer_from, cx_slice, make_runner, response
from mcp import Client

from jetbridge import draft, mcp_server, service
from jetbridge.alerts import AlertKind, DateState, confirm_opening, decide
from jetbridge.matcher import also_seen, evaluate
from jetbridge.models import (
    ANY_FLIGHT,
    AnswerKind,
    Cabin,
    CheckResult,
    CheckStatus,
    Flight,
    ParsedResponse,
    Query,
    SearchAnswer,
    UnreadableSlice,
    Watch,
)
from jetbridge.notify import email as mail
from jetbridge.runner import TickReport
from jetbridge.scheduler import opens_on

QUERY = Query("JFK", "HKG", DAY)
WATCH = Watch(1, ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS)


def flight(number: str, miles: int | None = 110_000) -> dict:
    return cx_slice(business_miles=miles, flight=number)


def add_any(store, clock, dates=(DAY,), **kwargs):
    return store.add_watch(
        ANY_FLIGHT, "JFK", "HKG", tuple(dates), Cabin.BUSINESS, now=clock(), **kwargs
    )


def connection() -> dict:
    two_legs = flight("500")
    second = dict(two_legs["segments"][0])
    second["flight"] = {"carrierCode": "CX", "flightNumber": "501"}
    two_legs["segments"] = [two_legs["segments"][0], second]
    return two_legs


# ---- naming it ------------------------------------------------------------------------
@pytest.mark.parametrize("text", ["any", "ANY", "any nonstop", "Any  Nonstop Flight", "any flight"])
def test_any_is_understood(text):
    assert Flight.parse(text).is_any


def test_a_real_flight_is_never_any():
    assert not Flight.parse("AA100").is_any
    with pytest.raises(ValueError):
        Flight.parse("ANY1")


def test_it_reads_plainly():
    assert WATCH.name == "any nonstop" and WATCH.label == "any nonstop JFK-HKG business"


def test_it_survives_being_saved(store, clock):
    watch = add_any(store, clock)
    assert store.get_watch(watch.id).flight.is_any


# ---- judging an answer ----------------------------------------------------------------
def test_the_cheapest_open_flight_is_shown_with_the_rest():
    answer = answer_from(
        response(flight("805", 110_000), flight("889", 95_000), flight("831", None))
    )
    result = evaluate(WATCH, DAY, answer)
    assert result.status is CheckStatus.AVAILABLE and result.miles == 95_000
    assert result.flight == "CX889" and result.flights == ("CX805", "CX889")


def test_flights_listed_with_no_seats_are_not_offered():
    result = evaluate(WATCH, DAY, answer_from(response(flight("805", None))))
    assert result.status is CheckStatus.NOT_OFFERED and result.flights == ()


def test_no_nonstop_at_all_is_not_listed():
    assert evaluate(WATCH, DAY, answer_from(response())).status is CheckStatus.NOT_LISTED


def test_a_connection_never_counts():
    result = evaluate(WATCH, DAY, answer_from(response(connection())))
    assert result.status is CheckStatus.NOT_LISTED


def test_a_leg_in_another_cabin_never_counts():
    answer = answer_from(response(cx_slice(leg_cabin="COACH")))
    assert evaluate(WATCH, DAY, answer).status is CheckStatus.NOT_OFFERED


def test_the_price_limit_applies_to_each_flight():
    limited = replace(WATCH, max_miles=100_000)
    answer = answer_from(response(flight("805", 110_000), flight("889", 95_000)))
    result = evaluate(limited, DAY, answer)
    assert result.miles == 95_000 and result.flights == ("CX889",)


def test_all_over_the_limit_is_a_near_miss():
    limited = replace(WATCH, max_miles=90_000)
    answer = answer_from(response(flight("805", 110_000), flight("889", 95_000)))
    result = evaluate(limited, DAY, answer)
    assert result.status is CheckStatus.NOT_OFFERED and result.miles == 95_000


def test_an_unreadable_result_stops_it_claiming_no_seats():
    parsed = ParsedResponse(unreadable=[UnreadableSlice(frozenset({"CX1"}), "odd")])
    answer = SearchAnswer(AnswerKind.ANSWER, parsed=parsed)
    assert evaluate(WATCH, DAY, answer).status is CheckStatus.UNKNOWN


@pytest.mark.parametrize(
    "answer",
    [
        SearchAnswer(AnswerKind.BLOCKED, detail="x"),
        SearchAnswer(AnswerKind.ERROR, detail="x"),
        SearchAnswer(AnswerKind.DRIFT, detail="x"),
        SearchAnswer(AnswerKind.ANSWER, parsed=ParsedResponse(site_error="309")),
    ],
)
def test_no_clear_answer_is_unknown(answer):
    assert evaluate(WATCH, DAY, answer).status is CheckStatus.UNKNOWN


def test_it_doesnt_mention_other_flights_because_it_watches_them():
    assert also_seen(WATCH, QUERY, answer_from(response(flight("889")))) == []


# ---- rules for several flights ---------------------------------------------------------
def _open(*flights: str, miles: int = 110_000) -> CheckResult:
    return CheckResult(
        CheckStatus.AVAILABLE, miles=miles, seats_at_least=1, flight=flights[0], flights=flights
    )


STATE = DateState(
    status=CheckStatus.AVAILABLE, miles=110_000, open_flights=("CX805",), last_alert_at=0
)


def test_another_flight_opening_gets_a_re_check():
    decision = decide(STATE, _open("CX805", "CX889"), 10)
    assert decision.needs_confirmation and not decision.alerts
    assert decision.state.status is CheckStatus.AVAILABLE


def test_the_same_flights_staying_open_is_quiet():
    decision = decide(STATE, _open("CX805"), 10)
    assert not decision.needs_confirmation and not decision.alerts


def test_a_confirmed_new_flight_alerts():
    decision = confirm_opening(STATE, _open("CX805", "CX889"), _open("CX805", "CX889"), 20)
    assert decision.alerts == (AlertKind.SEATS_OPEN,)
    assert decision.state.open_flights == ("CX805", "CX889")


def test_a_new_flight_that_vanished_by_the_re_check_is_quiet():
    decision = confirm_opening(STATE, _open("CX805", "CX889"), _open("CX805"), 20)
    assert not decision.alerts and decision.state.status is CheckStatus.AVAILABLE


def test_everything_gone_by_the_re_check_says_so():
    gone = CheckResult(CheckStatus.NOT_OFFERED, detail="no business seats")
    decision = confirm_opening(STATE, _open("CX805", "CX889"), gone, 20)
    assert decision.alerts == (AlertKind.SEATS_GONE,)
    assert decision.state.open_flights == ()


def test_one_flight_closing_while_another_stays_is_quiet():
    state = replace(STATE, open_flights=("CX805", "CX889"))
    decision = decide(state, _open("CX889"), 10)
    assert not decision.needs_confirmation and decision.state.open_flights == ("CX889",)


def test_a_flight_that_closed_and_reopened_is_news_again():
    state = decide(replace(STATE, open_flights=("CX805", "CX889")), _open("CX889"), 10).state
    assert decide(state, _open("CX805", "CX889"), 20).needs_confirmation


def test_an_exact_watch_behaves_as_before():
    exact = CheckResult(CheckStatus.AVAILABLE, miles=110_000, seats_at_least=1)
    state = DateState(status=CheckStatus.AVAILABLE, miles=110_000, last_alert_at=0)
    assert not decide(state, exact, 10).needs_confirmation


# ---- through real ticks ---------------------------------------------------------------
async def test_each_flight_gets_its_own_alert_naming_it(store, clock):
    add_any(store, clock)
    first = answer_from(response(flight("805"), flight("889", None)))
    both = answer_from(response(flight("805"), flight("889", 95_000)))
    runner, fetcher, sender, _ = make_runner(store, clock, [first, first, both, both])
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert sender.subjects[0].startswith("SEATS OPEN: CX805 business")
    assert sender.subjects[1].startswith("SEATS OPEN: CX889 business")
    assert "Also open: CX805" in sender.sent[1][1]
    assert len(fetcher.queries) == 4  # a search and a re-check each time


def test_first_check_wording_says_what_any_means():
    assert "no nonstop" in service.first_check_means(WATCH, CheckResult(CheckStatus.NOT_LISTED))
    assert "lists nonstops" in service.first_check_means(
        WATCH, CheckResult(CheckStatus.NOT_OFFERED)
    )
    assert "on CX805, CX889" in service.first_check_means(WATCH, _open("CX805", "CX889"))


# ---- saving ---------------------------------------------------------------------------
FUTURE = (date.today() + timedelta(days=30)).isoformat()


def test_the_review_says_any_and_that_connections_dont_count():
    text = draft.review_text(draft.WatchDraft("JFK", "HKG", FUTURE, "ANY"), "me@x.com")
    assert "any nonstop flight from JFK to HKG" in text and "Connections don't count" in text


def test_a_command_can_add_one(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    store = service.open_store()
    watch = service.create_watch(
        store, flight="any", origin="JFK", destination="HKG", dates=FUTURE, cabin="business"
    )
    assert watch.flight.is_any and watch.name == "any nonstop"


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: TickReport())
    monkeypatch.setattr(mail, "load_config", lambda: mail.EmailConfig("o@x.com", "me@x.com"))
    monkeypatch.setattr(mail.keyring, "get_password", lambda *_: "secret")
    monkeypatch.setattr(mcp_server.autorun, "is_installed", lambda: True)
    mcp_server._checks.clear()
    service.open_store().set(service.RISK_ACCEPTED, "1")


DETAILS = {
    "origin": "JFK",
    "destination": "HKG",
    "dates": FUTURE,
    "program": "american",
    "pax": 1,
}


async def _review_and_save(**fields):
    async with Client(mcp_server.server) as client:
        review = await client.call_tool("review_watch", {**DETAILS, **fields})
        saved = await client.call_tool(
            "confirm_watch", {"review_id": review.structured_content["review_id"]}
        )
    return review.structured_content, saved.structured_content


async def test_several_flights_and_cabins_are_one_review(chat):
    review, saved = await _review_and_save(flight="CX805 or CX889", cabin="business or first")
    assert "CX805, CX889 from JFK" in review["review"] and "business, first" in review["review"]
    assert "4 watches sharing these searches" in review["review"]
    watches = service.open_store().list_watches()
    assert len(watches) == 4 and len(saved["also_saved"]) == 3
    assert {(w.flight.code, w.cabin) for w in watches} == {
        ("CX805", Cabin.BUSINESS),
        ("CX805", Cabin.FIRST),
        ("CX889", Cabin.BUSINESS),
        ("CX889", Cabin.FIRST),
    }


async def test_several_share_one_search_each_date(chat):
    await _review_and_save(flight="CX805, CX889", cabin="business")
    from jetbridge.scheduler import group_queries

    groups = group_queries(service.open_store().list_watches(), date.today())
    assert len(groups) == 1 and len(next(iter(groups.values()))) == 2


async def test_a_single_watch_reads_the_same_as_before(chat):
    review, saved = await _review_and_save(flight="CX805", cabin="business")
    assert "sharing" not in review["review"] and "also_saved" not in saved


async def test_any_works_from_chat(chat):
    review, saved = await _review_and_save(flight="any nonstop", cabin="business")
    assert "any nonstop flight" in review["review"] and saved["flight"] == "any nonstop"


async def test_premium_economy_stays_one_cabin(chat):
    review, _ = await _review_and_save(flight="CX805", cabin="premium economy")
    assert "Cabin:       premium" in review["review"]


async def test_too_many_at_once_is_refused(chat):
    async with Client(mcp_server.server) as client:
        result = await client.call_tool(
            "review_watch",
            {**DETAILS, "flight": "CX1, CX2, CX3, CX4", "cabin": "business, first"},
        )
    assert result.is_error and "at once" in result.content[0].text


async def test_one_bad_flight_saves_none_of_them(chat):
    async with Client(mcp_server.server) as client:
        result = await client.call_tool(
            "review_watch", {**DETAILS, "flight": "CX805, nonsense", "cabin": "business"}
        )
    assert result.is_error and service.open_store().list_watches() == []


# ---- the reminder ---------------------------------------------------------------------
def _reminder_watch(store, clock, opens_in_days: int, **kwargs):
    today = datetime.fromtimestamp(clock()).date()
    day = today + timedelta(days=331 + opens_in_days)
    return store.add_watch(
        Flight("CX", "805"), "JFK", "HKG", (day,), Cabin.BUSINESS, now=clock(), **kwargs
    ), day


def _reminders(sender: FakeSender) -> list[str]:
    return [subject for subject in sender.subjects if "should open" in subject]


def test_a_date_opens_by_the_programs_own_window():
    assert opens_on(date(2027, 12, 1), "american") == date(2027, 12, 1) - timedelta(days=331)
    assert opens_on(date(2027, 12, 1), "united") == date(2027, 12, 1) - timedelta(days=337)


@pytest.mark.parametrize(
    ("opens_in_days", "expected"),
    [(2, None), (1, "tomorrow"), (0, "today"), (-1, None)],
)
async def test_the_reminder_comes_the_day_before(store, clock, opens_in_days, expected):
    _reminder_watch(store, clock, opens_in_days)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response())])
    await runner.tick()
    found = _reminders(sender)
    if expected is None:
        assert found == []
    else:
        assert len(found) == 1 and f"should open {expected}" in found[0]


async def test_the_reminder_comes_once(store, clock):
    _reminder_watch(store, clock, 1)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response())] * 4)
    await runner.tick()
    clock.now += 3600
    await runner.tick()
    clock.now += 86400
    await runner.tick()
    assert len(_reminders(sender)) == 1


async def test_the_reminder_takes_no_searches(store, clock):
    # 2 days before opening is also 5 days before anything is searchable, so nothing else is due.
    today = datetime.fromtimestamp(clock()).date()
    day = today + timedelta(days=332)
    store.add_watch(Flight("CX", "805"), "JFK", "HKG", (day,), Cabin.BUSINESS, now=clock())
    store.set_next_check(1, clock() + 10**7)
    runner, fetcher, sender, _ = make_runner(store, clock, [])
    await runner.tick()
    assert fetcher.queries == [] and len(_reminders(sender)) == 1


async def test_each_person_on_the_watch_gets_it(store, clock):
    _reminder_watch(store, clock, 1, recipients=("ann@x.com", "bo@y.com"))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response())])
    await runner.tick()
    assert sorted(
        to for to, s in zip(sender.to, sender.subjects, strict=True) if "should open" in s
    ) == [
        "ann@x.com",
        "bo@y.com",
    ]


async def test_the_reminder_says_it_might_be_a_day_out_and_promises_nothing(store, clock):
    _reminder_watch(store, clock, 1)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response())])
    await runner.tick()
    body = next(b for s, b in sender.sent if "should open" in s)
    assert "a day or so out" in body and "aren't guaranteed" in body


async def test_a_paused_watch_gets_no_reminder(store, clock):
    watch, _ = _reminder_watch(store, clock, 1)
    store.set_watch_status(watch.id, "paused")
    runner, _, sender, _ = make_runner(store, clock, [])
    await runner.tick()
    assert _reminders(sender) == []
