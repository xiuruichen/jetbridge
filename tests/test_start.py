"""`jetbridge start`: friendly input, the flight picker, and nothing saved without a yes."""

from __future__ import annotations

from datetime import date, datetime

import pytest
from conftest import ScriptedFetcher, answer_from, cx_slice, response
from typer.testing import CliRunner

from jetbridge import cli, draft, service, winsched
from jetbridge.cli import app
from jetbridge.models import AnswerKind, Cabin, CheckResult, CheckStatus, Query, SearchAnswer
from jetbridge.notify import email as mail
from jetbridge.runner import CheckLine, TickReport
from jetbridge.service import FlightOption, FlightSearch, TickBusy, open_store

TODAY = date(2026, 11, 1)
DAY = date(2027, 5, 21)
QUERY = Query("JFK", "HKG", DAY)
OPEN_RESULT = service.CheckResult(CheckStatus.AVAILABLE, miles=110_000)
CLOSED_RESULT = service.CheckResult(CheckStatus.NOT_OFFERED)
OPTION = FlightOption(
    "CX845",
    datetime(2027, 5, 21, 1, 0),
    datetime(2027, 5, 22, 5, 0),
    0,
    CLOSED_RESULT,
)
OTHER = FlightOption(
    "CX831",
    datetime(2027, 5, 21, 9, 0),
    datetime(2027, 5, 22, 14, 0),
    0,
    OPEN_RESULT,
)
cli_runner = CliRunner()


@pytest.fixture(autouse=True)
def ready(tmp_path, monkeypatch):
    """A set-up install with a fake clock date and no real Chrome, task or Gmail."""
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(mail, "load_config", lambda: mail.EmailConfig("o@x.com", "owner@x.com"))
    monkeypatch.setattr(mail.keyring, "get_password", lambda *a: "secret")
    monkeypatch.setattr(winsched, "is_installed", lambda: True)
    monkeypatch.setattr(cli, "date", type("D", (), {"today": staticmethod(lambda: TODAY)}))
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: TickReport())

    class Pinned(date):
        @classmethod
        def today(cls):
            return TODAY

    monkeypatch.setattr(draft, "date", Pinned)
    open_store().set("risk_accepted", "1")


def picker(monkeypatch, search):
    calls = []

    def fake(store, query, cabin):
        calls.append((query, cabin))
        if isinstance(search, Exception):
            raise search
        return search

    monkeypatch.setattr(service, "find_flights", fake)
    return calls


def lines(*answers) -> str:
    return "\n".join(answers) + "\n"


def watches():
    return open_store().list_watches()


# ---- friendly input -------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("May 21-23 2027", "2027-05-21..2027-05-23"),
        ("May 21st to May 23rd, 2027", "2027-05-21..2027-05-23"),
        ("may 21, 2027", "2027-05-21"),
        ("May 21, May 23 2027", "2027-05-21,2027-05-23"),
        ("May 21, 23 2027", "2027-05-21,2027-05-23"),
        ("May 28 - Jun 2 2027", "2027-05-28..2027-06-02"),
        ("Dec 30 - Jan 2 2027", "2026-12-30..2027-01-02"),
        ("Sept 5 2027", "2027-09-05"),
        ("May 21", "2027-05-21"),
        ("Dec 25", "2026-12-25"),
        ("Dec 30 - Jan 2", "2026-12-30..2027-01-02"),
        ("2027-05-21..2027-05-23", "2027-05-21..2027-05-23"),
    ],
)
def test_friendly_dates(text, expected):
    assert draft.parse_dates(text, TODAY) == expected


@pytest.mark.parametrize(
    "text",
    ["", "soon", "5/21/2027", "21", "Smarch 3 2027", "Feb 30 2027", "May 1-30 2027", "2027-13-01"],
)
def test_unclear_dates_are_refused(text):
    with pytest.raises(ValueError):
        draft.parse_dates(text, TODAY)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hong Kong", ("HKG",)),
        ("  hong   kong ", ("HKG",)),
        ("NYC", ("JFK", "EWR", "LGA")),
        ("Toronto", ("YYZ", "YTZ")),
        ("yyz", ("YYZ",)),
        ("Narnia", ()),
        ("", ()),
    ],
)
def test_airports(text, expected):
    assert draft.resolve_airport(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [("120k", 120_000), ("120,000", 120_000), ("1.5k", 1500), ("80000 miles", 80_000), ("", None)],
)
def test_miles(text, expected):
    assert draft.parse_miles(text) == expected


@pytest.mark.parametrize("text", ["0", "lots", "-5", "1.5"])
def test_bad_miles_are_refused(text):
    with pytest.raises(ValueError):
        draft.parse_miles(text)


def test_cabin_names():
    assert draft.parse_cabin("Business Class") is Cabin.BUSINESS
    assert draft.parse_cabin("premium economy") is Cabin.PREMIUM
    with pytest.raises(ValueError):
        draft.parse_cabin("cargo")


def test_review_shows_the_cost_and_warns_when_high():
    small = draft.WatchDraft("JFK", "HKG", "2027-05-21..2027-05-23", "CX845")
    assert "about 9 aa.com searches a day, up to 36" in draft.review_text(small, "owner@x.com")
    assert "owner@x.com" in draft.review_text(small, "owner@x.com")
    assert "block more likely" not in draft.review_text(small, "owner@x.com")

    large = draft.WatchDraft("JFK", "HKG", "2027-05-21..2027-06-03", "CX845")
    assert "about 42 aa.com searches" in draft.review_text(large, "o")
    assert "limit of 150 requests a day" in draft.review_text(large, "o")

    busy = draft.WatchDraft("JFK", "HKG", "2027-05-21..2027-05-24", "CX845")
    assert "block more likely" in draft.review_text(busy, "o")


def test_saving_an_unfinished_draft_is_refused():
    with pytest.raises(ValueError, match="flight"):
        draft.save(open_store(), draft.WatchDraft("JFK", "HKG", "2027-05-21"))
    assert watches() == []


# ---- flight picker --------------------------------------------------------------------
def real_slice(flight: str, day: str = "2027-05-21", **kwargs) -> dict:
    return cx_slice(flight=flight, day=day, **kwargs)


def connection() -> dict:
    item = real_slice("845")
    leg = dict(item["segments"][0]["legs"][0])
    second = {"flight": {"carrierCode": "CX", "flightNumber": "100"}, "legs": [leg]}
    return {**item, "segments": [item["segments"][0], second]}


def test_flights_are_read_with_the_rules_a_watch_uses():
    answer = answer_from(
        response(
            real_slice("845", business_miles=None),
            real_slice("831", business_miles=110_000),
            real_slice("999", day="2027-05-22"),  # the wrong date
            connection(),  # more than one flight number
        )
    )
    found = service.flights_from_answer(answer, QUERY, Cabin.BUSINESS)
    assert found.problem is None
    assert {o.code: o.result.status for o in found.options} == {
        "CX845": CheckStatus.NOT_OFFERED,
        "CX831": CheckStatus.AVAILABLE,
    }


def test_economy_watch_of_the_same_flight_is_judged_on_economy():
    answer = answer_from(response(real_slice("845", business_miles=None)))
    found = service.flights_from_answer(answer, QUERY, Cabin.ECONOMY)
    assert found.options[0].result.status is CheckStatus.AVAILABLE


@pytest.mark.parametrize(
    "kind",
    [AnswerKind.BLOCKED, AnswerKind.DEFERRED, AnswerKind.DRIFT, AnswerKind.ERROR],
)
def test_no_answer_is_a_problem_not_an_empty_list(kind):
    found = service.flights_from_answer(SearchAnswer(kind), QUERY, Cabin.BUSINESS)
    assert found.options == () and found.problem


def test_site_error_is_a_problem():
    answer = answer_from(response(error="309"))
    assert service.flights_from_answer(answer, QUERY, Cabin.BUSINESS).problem


async def test_a_blocked_search_is_not_retried():
    fetcher = ScriptedFetcher([SearchAnswer(AnswerKind.BLOCKED, detail="challenge")])
    found = await service.search_flights(fetcher, QUERY, Cabin.BUSINESS, deadline=1e12)
    assert found.problem and len(fetcher.queries) == 1 and fetcher.closed == 1


# ---- the wizard -----------------------------------------------------------------------
ROUTE = ("JFK", "HKG", "May 21-23 2027", "business", "2", "american")


def test_picking_a_flight_saves_exactly_what_was_reviewed(monkeypatch):
    calls = picker(monkeypatch, FlightSearch((OPTION, OTHER)))
    answers = lines(*ROUTE, "", "2", "120k", "ann@example.com, bo@example.org", "y", "n")
    result = cli_runner.invoke(app, ["start"], input=answers)

    assert result.exit_code == 0, result.output
    assert calls == [(Query("JFK", "HKG", DAY, 2), Cabin.BUSINESS)]  # the real party size
    assert "no business seats right now" in result.output
    assert "business seats open right now: 110,000 miles" in result.output
    assert "not_offered" not in result.output
    assert "about 9 aa.com searches a day" in result.output

    watch = watches()[0]
    assert watch.flight.code == "CX831" and (watch.origin, watch.destination) == ("JFK", "HKG")
    assert len(watch.dates) == 3 and watch.pax == 2 and watch.max_miles == 120_000
    assert watch.recipients == ("ann@example.com", "bo@example.org")


def test_a_known_flight_number_skips_the_search(monkeypatch):
    calls = picker(monkeypatch, AssertionError("should not search"))
    answers = lines(*ROUTE, "cx 0845", "", "", "y", "n")
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    assert calls == []
    assert watches()[0].flight.code == "CX845" and watches()[0].recipients == ()


@pytest.mark.parametrize("answer", ["n", ""])
def test_nothing_is_saved_without_a_yes(monkeypatch, answer):
    picker(monkeypatch, FlightSearch((OPTION,)))
    answers = lines(*ROUTE, "", "1", "", "", answer, "n")
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    assert "nothing was saved" in result.output and watches() == []


def test_stopping_part_way_saves_nothing(monkeypatch):
    picker(monkeypatch, FlightSearch((OPTION,)))
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, ""))
    assert result.exit_code == 1 and watches() == []


def test_a_blocked_search_says_so_and_only_searches_once(monkeypatch):
    calls = picker(monkeypatch, FlightSearch(problem="aa.com asked jetbridge to slow down"))
    answers = lines(*ROUTE, "", "", "n")
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    assert "slow down" in result.output and len(calls) == 1 and watches() == []


def test_a_blocked_search_still_allows_a_typed_flight(monkeypatch):
    picker(monkeypatch, FlightSearch(problem="aa.com asked jetbridge to slow down"))
    answers = lines(*ROUTE, "", "CX845", "", "", "y", "n")
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    assert watches()[0].flight.code == "CX845"


def test_an_empty_list_doesnt_claim_the_flight_is_missing(monkeypatch):
    picker(monkeypatch, FlightSearch())
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "", "", "n"))
    assert "may still exist" in result.output and watches() == []


def test_a_running_check_doesnt_crash_the_questions(monkeypatch):
    picker(monkeypatch, TickBusy("busy"))
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "", "", "n"))
    assert result.exit_code == 0, result.output
    assert "using Chrome" in result.output


def test_bad_answers_get_asked_again(monkeypatch):
    picker(monkeypatch, FlightSearch((OPTION,)))
    answers = lines(
        "Narnia",  # unknown city
        "Toronto",  # two airports
        "1",
        "Toronto",  # the same airport as the origin, once chosen
        "YYZ",
        "YYZ",
        "HKG",
        "5/21/2027",  # ambiguous
        "May 21 2020",  # in the past
        "May 21 2027",
        "cargo",
        "business",
        "99",
        "1",
        "alaska",  # not supported
        "UA",
        "",
        "7",  # not on the list
        "1",
        "lots",
        "",
        "not an email",
        "ann@example.com",
        "y",
        "n",
    )
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    watch = watches()[0]
    assert (watch.origin, watch.destination) == ("YYZ", "HKG")
    assert watch.flight.code == "CX845" and watch.recipients == ("ann@example.com",)
    assert watch.program == "united"


def test_add_another_keeps_going(monkeypatch):
    picker(monkeypatch, FlightSearch((OPTION,)))
    answers = lines(
        *ROUTE, "CX845", "", "", "y", "y", "JFK", "LHR", "Jun 1 2027", "first", "1", "delta",
        "BA178", "", "", "y", "n",
    )  # fmt: skip
    result = cli_runner.invoke(app, ["start"], input=answers)
    assert result.exit_code == 0, result.output
    assert [w.flight.code for w in watches()] == ["CX845", "BA178"]


# ---- risk notice ----------------------------------------------------------------------
def test_declining_the_notice_sets_up_and_searches_nothing(monkeypatch):
    calls = picker(monkeypatch, AssertionError("should not search"))
    setup = []
    monkeypatch.setattr(cli, "notify_setup", lambda: setup.append("setup"))
    open_store().set("risk_accepted", "0")
    for answer in ("n", ""):
        result = cli_runner.invoke(app, ["start"], input=lines(answer))
        assert result.exit_code == 0, result.output
        assert "terms of use" in result.output and "logged in" in result.output
        assert "nothing was set up" in result.output
    assert calls == [] and setup == [] and watches() == []
    assert open_store().get("risk_accepted", "0") == "0"


def test_the_notice_is_shown_once(monkeypatch):
    picker(monkeypatch, FlightSearch((OPTION,)))
    open_store().set("risk_accepted", "0")
    first = cli_runner.invoke(app, ["start"], input=lines("y", *ROUTE, "CX845", "", "", "n", "n"))
    assert first.exit_code == 0, first.output
    assert "terms of use" in first.output

    second = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "n", "n"))
    assert second.exit_code == 0, second.output
    assert "terms of use" not in second.output


def test_closing_at_the_notice_accepts_nothing():
    open_store().set("risk_accepted", "0")
    result = cli_runner.invoke(app, ["start"], input="")
    assert result.exit_code == 1
    assert open_store().get("risk_accepted", "0") == "0"


# ---- first run ------------------------------------------------------------------------
def first_run(monkeypatch):
    done = []
    monkeypatch.setattr(winsched, "is_installed", lambda: False)
    monkeypatch.setattr(winsched, "install", lambda: done.append("install"))
    monkeypatch.setattr(cli, "doctor", lambda: done.append("doctor"))
    return done


def test_first_run_turns_on_checks_only_after_a_watch_is_added(monkeypatch):
    done = first_run(monkeypatch)
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "y", "n"))
    assert result.exit_code == 0, result.output
    assert done == ["install", "doctor"]


def test_first_run_with_nothing_added_changes_nothing(monkeypatch):
    done = first_run(monkeypatch)
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "n", "n"))
    assert result.exit_code == 0, result.output
    assert done == []


def test_later_runs_skip_setup(monkeypatch):
    called = []
    monkeypatch.setattr(cli, "notify_setup", lambda: called.append("setup"))
    monkeypatch.setattr(winsched, "install", lambda: called.append("install"))
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "y", "n"))
    assert result.exit_code == 0, result.output
    assert called == []


def test_failed_email_stops_and_asks_again_next_time(monkeypatch):
    import typer

    calls = []

    def failing_setup():
        calls.append("setup")
        raise typer.Exit(code=1)

    monkeypatch.setattr(mail, "load_config", lambda: None)
    monkeypatch.setattr(cli, "notify_setup", failing_setup)
    first = cli_runner.invoke(app, ["start"])
    assert first.exit_code == 1 and "no flight was added" in first.output and watches() == []

    # The bad details are saved by then, but the next run mustn't treat email as working.
    monkeypatch.setattr(mail, "load_config", lambda: mail.EmailConfig("o@x.com", "o@x.com"))
    second = cli_runner.invoke(app, ["start"])
    assert second.exit_code == 1 and calls == ["setup", "setup"]


def test_search_count_leaves_out_dates_that_have_passed(monkeypatch):
    class Yesterdayish(date):
        @classmethod
        def today(cls):
            return cls(2027, 6, 15)

    monkeypatch.setattr("jetbridge.draft.date", Yesterdayish)
    watch = draft.WatchDraft(
        origin="JFK",
        destination="LHR",
        dates="2027-06-14..2027-06-16",
        flight="AA100",
        checks_per_day=6,
    )
    assert watch.searches_per_day == 24  # two dates left, each in its last two weeks


def test_start_checks_a_new_watch_and_says_what_it_means(monkeypatch):
    def tick(store, only_watch=None):
        watch = store.get_watch(only_watch)
        result = CheckResult(CheckStatus.NOT_LISTED)
        return TickReport(checks=[CheckLine(watch.id, watch.label, watch.dates[0], result)])

    monkeypatch.setattr(service, "live_tick", tick)
    picker(monkeypatch, FlightSearch())
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "y", "n"))
    assert "wasn't in its results" in result.output


def test_a_failed_first_check_keeps_the_watch(monkeypatch):
    def tick(*_, **__):
        raise RuntimeError("Chrome wouldn't start")

    monkeypatch.setattr(service, "live_tick", tick)
    picker(monkeypatch, FlightSearch())
    result = cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "y", "n"))
    assert result.exit_code == 0 and "It'll try again" in result.output
    assert len(open_store().list_watches()) == 1


def test_no_first_check_when_nothing_was_saved(monkeypatch):
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: pytest.fail("searched"))
    picker(monkeypatch, FlightSearch())
    cli_runner.invoke(app, ["start"], input=lines(*ROUTE, "CX845", "", "", "n", "n"))
