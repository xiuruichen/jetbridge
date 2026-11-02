"""Everything the commands do has to work from a chat, in plain words."""

from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from mcp import Client
from typer.testing import CliRunner

from jetbridge import cli, mcp_server, service
from jetbridge.alerts import AlertKind
from jetbridge.models import CheckStatus
from jetbridge.notify import email as mail
from jetbridge.service import FlightOption, FlightSearch, open_store

FUTURE = date.today() + timedelta(days=200)
DETAILS = {
    "flight": "DL197",
    "origin": "seattle",
    "destination": "icn",
    "dates": FUTURE.isoformat(),
    "cabin": "business",
    "program": "delta",
    "pax": 1,
}
ROUTE = {k: DETAILS[k] for k in ("origin", "destination", "dates", "cabin", "program", "pax")}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(mail, "load_config", lambda: mail.EmailConfig("o@x.com", "owner@x.com"))
    monkeypatch.setattr(mail.keyring, "get_password", lambda *_: "secret")
    tasks = SimpleNamespace(on=True)
    monkeypatch.setattr(mcp_server.winsched, "is_installed", lambda: tasks.on)
    monkeypatch.setattr(mcp_server.winsched, "install", lambda: setattr(tasks, "on", True))
    monkeypatch.setattr(mcp_server.winsched, "remove", lambda: setattr(tasks, "on", False))
    for pending in (mcp_server._reviews, mcp_server._removals, mcp_server._checks):
        pending.clear()
    open_store().set(service.RISK_ACCEPTED, "1")
    return tasks


@pytest.fixture
def watch():
    return service.create_watch(
        open_store(),
        flight="DL197",
        origin="SEA",
        destination="ICN",
        dates=FUTURE.isoformat(),
        cabin="economy",
        program="delta",
    ).id


async def call(tool, **arguments):
    async with Client(mcp_server.server) as client:
        result = await client.call_tool(tool, arguments)
    assert not result.is_error, result
    return result.structured_content


async def refused(tool, **arguments) -> str:
    async with Client(mcp_server.server) as client:
        result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


async def test_every_command_has_a_way_in_from_chat():
    async with Client(mcp_server.server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert names == {
        "get_started",
        "accept_notice",
        "open_email_setup",
        "send_test_email",
        "review_watch",
        "confirm_watch",
        "find_flights",
        "check_now",
        "check_result",
        "list_watches",
        "watch_history",
        "set_recipients",
        "pause_watch",
        "resume_watch",
        "review_watch_removal",
        "confirm_watch_removal",
        "alert_settings",
        "set_daily_summary",
        "set_alert_headline",
        "set_automatic_checks",
        "health",
        "diagnose",
    }


async def test_only_the_tools_that_change_nothing_say_so():
    async with Client(mcp_server.server) as client:
        tools = (await client.list_tools()).tools
    read_only = {tool.name for tool in tools if tool.annotations.read_only_hint}
    assert read_only == {
        "get_started",
        "check_result",
        "list_watches",
        "watch_history",
        "alert_settings",
        "health",
        "diagnose",
    }


# ---- plain words ----------------------------------------------------------------------
@pytest.mark.parametrize("program", ["delta", "Delta", "SkyMiles", "delta miles"])
async def test_people_can_name_the_miles_their_way(program):
    answer = await call("review_watch", **{**DETAILS, "program": program})
    assert "Delta SkyMiles" in answer["review"]


async def test_a_city_with_one_airport_just_works():
    answer = await call("review_watch", **DETAILS)
    assert "from SEA to ICN" in answer["review"]


async def test_a_city_with_several_airports_gets_a_question_not_a_guess():
    message = await refused("review_watch", **{**DETAILS, "destination": "Seoul"})
    assert "ICN, GMP" in message
    assert mcp_server._reviews == {}


async def test_an_unknown_place_says_so():
    assert "3-letter code" in await refused("review_watch", **{**DETAILS, "origin": "Narnia"})


# ---- getting started ------------------------------------------------------------------
async def test_first_time_shows_the_notice_and_nothing_searches_until_accepted(watch):
    open_store().delete(service.RISK_ACCEPTED)
    answer = await call("get_started")
    assert answer["notice_accepted"] is False
    assert "terms of use" in answer["notice"]
    assert "notice" in answer["next_step"]

    review = await call("review_watch", **DETAILS)
    assert "accept" in await refused("confirm_watch", review_id=review["review_id"])
    assert len(open_store().list_watches()) == 1
    assert "accept" in await refused("check_now", watch_id=watch)
    assert "accept" in await refused("find_flights", **ROUTE)
    assert "accept" in await refused("set_automatic_checks", on=True)

    await call("accept_notice")
    assert (await call("get_started"))["notice_accepted"] is True


async def test_the_notice_is_the_same_one_the_commands_show(monkeypatch):
    open_store().delete(service.RISK_ACCEPTED)
    assert (await call("get_started"))["notice"] == service.RISK_NOTICE
    result = CliRunner().invoke(cli.app, ["start"], input="n\n")
    assert service.RISK_NOTICE.splitlines()[0] in result.output


@pytest.mark.parametrize(
    ("state", "phrase"),
    [
        ("no_email", "open_email_setup"),
        ("no_watch", "Ask which flight"),
        ("no_checks", "automatic checks"),
        ("ready", "Everything is set up"),
    ],
)
async def test_get_started_says_what_is_next(state, phrase, isolated, monkeypatch):
    if state != "no_watch":
        service.create_watch(
            open_store(),
            flight="DL197",
            origin="SEA",
            destination="ICN",
            dates=FUTURE.isoformat(),
            cabin="economy",
        )
    if state == "no_email":
        monkeypatch.setattr(mail, "load_config", lambda: None)
    if state == "no_checks":
        isolated.on = False
    assert phrase in (await call("get_started"))["next_step"]


async def test_the_first_watch_turns_on_automatic_checks(isolated):
    isolated.on = False
    review = await call("review_watch", **DETAILS)
    saved = await call("confirm_watch", review_id=review["review_id"])
    assert isolated.on is True
    assert "Automatic checks are now on" in saved["next_steps"][0]


async def test_a_watch_saved_without_email_says_alerts_will_wait(monkeypatch):
    monkeypatch.setattr(mail, "load_config", lambda: None)
    review = await call("review_watch", **DETAILS)
    saved = await call("confirm_watch", review_id=review["review_id"])
    assert any("alerts will wait" in step for step in saved["next_steps"])


async def test_email_setup_opens_a_window_and_never_takes_a_password(monkeypatch):
    opened = []
    monkeypatch.setattr(
        mcp_server.subprocess, "Popen", lambda args, **kwargs: opened.append((args, kwargs))
    )
    async with Client(mcp_server.server) as client:
        tools = {tool.name: tool for tool in (await client.list_tools()).tools}
        await client.call_tool("open_email_setup", {})
    assert tools["open_email_setup"].input_schema.get("properties", {}) == {}
    args, kwargs = opened[0]
    assert args[1:] == ["-m", "jetbridge", "notify", "setup-window"]
    assert kwargs["creationflags"] == mcp_server.subprocess.CREATE_NEW_CONSOLE
    for tool in tools.values():
        assert not any("password" in name for name in tool.input_schema.get("properties", {}))


def test_the_setup_window_waits_after_a_failure(monkeypatch):
    def fails():
        raise cli.typer.Exit(code=1)

    monkeypatch.setattr(cli, "notify_setup", fails)
    result = CliRunner().invoke(cli.app, ["notify", "setup-window"], input="\n")
    assert "didn't work" in result.output
    assert "Press Enter" in result.output


async def test_test_email_goes_where_asked(monkeypatch):
    sent = []
    monkeypatch.setattr(mail.GmailSender, "send", lambda self, s, b, to=None: sent.append(to))
    assert (await call("send_test_email"))["sent_to"] == "owner@x.com"
    assert (await call("send_test_email", to="ann@x.com"))["sent_to"] == "ann@x.com"
    assert sent == [None, "ann@x.com"]
    assert "one address" in await refused("send_test_email", to="a@x.com,b@x.com")
    assert "not an email" in await refused("send_test_email", to="nope")


async def test_test_email_failure_says_why(monkeypatch):
    def fail(self, subject, body, to=None):
        raise OSError("login refused")

    monkeypatch.setattr(mail.GmailSender, "send", fail)
    assert "login refused" in await refused("send_test_email")


# ---- finding a flight -----------------------------------------------------------------
def option(code):
    return FlightOption(
        code,
        datetime(2027, 5, 21, 1, 0),
        datetime(2027, 5, 22, 5, 0),
        0,
        service.CheckResult(CheckStatus.AVAILABLE, miles=110_000),
    )


async def test_flights_are_listed_with_the_caveat(monkeypatch):
    queries = []

    def found(store, query, cabin):
        queries.append(query)
        return FlightSearch((option("DL197"),))

    monkeypatch.setattr(service, "find_flights", found)
    answer = await call("find_flights", **ROUTE)
    assert answer["finished"] is True
    assert answer["flights"][0]["flight"] == "DL197"
    assert "110,000 miles" in answer["flights"][0]["seats"]
    assert "may still exist" in answer["note"]
    assert (queries[0].origin, queries[0].destination, queries[0].program) == (
        "SEA",
        "ICN",
        "delta",
    )


async def test_a_lookup_that_got_no_answer_says_so_rather_than_no_flights(monkeypatch):
    monkeypatch.setattr(
        service, "find_flights", lambda *_: FlightSearch(problem="delta.com asked to slow down")
    )
    answer = await call("find_flights", **ROUTE)
    assert answer["problem"] == "delta.com asked to slow down"


async def test_a_slow_lookup_is_not_searched_twice(monkeypatch):
    release, calls = threading.Event(), []

    def slow(store, query, cabin):
        calls.append(query)
        release.wait(10)
        return FlightSearch((option("DL197"),))

    monkeypatch.setattr(service, "find_flights", slow)
    monkeypatch.setattr(mcp_server, "CHECK_WAIT_S", 0.05)
    first = await call("find_flights", **ROUTE)
    again = await call("find_flights", **ROUTE)
    assert first["finished"] is False
    assert again["check_id"] == first["check_id"]
    release.set()
    monkeypatch.setattr(mcp_server, "CHECK_WAIT_S", 5)
    done = await call("check_result", check_id=first["check_id"])
    assert done["flights"][0]["flight"] == "DL197"
    assert len(calls) == 1


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"destination": "SEA"}, "are the same"),
        ({"dates": "2020-01-01"}, "have passed"),
        ({"pax": 0}, "pax must be 1-9"),
        ({"cabin": "luxury"}, "economy, premium, business or first"),
    ],
)
async def test_a_bad_lookup_searches_nothing_and_says_why(change, reason, monkeypatch):
    monkeypatch.setattr(service, "find_flights", lambda *_: pytest.fail("searched"))
    assert reason in await refused("find_flights", **{**ROUTE, **change})


# ---- checks ---------------------------------------------------------------------------
async def test_a_paused_watch_is_not_checked(watch, monkeypatch):
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: pytest.fail("searched"))
    await call("pause_watch", watch_id=watch)
    assert "paused" in await refused("check_now", watch_id=watch)


async def test_a_check_that_could_not_run_says_why(watch, monkeypatch):
    report = SimpleNamespace(checks=[], alerts_sent=0, stopped_early="delta.com is resting")
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: report)
    answer = await call("check_now", watch_id=watch)
    assert answer["stopped_early"] == "delta.com is resting"
    assert answer["results"] == []


# ---- managing watches -----------------------------------------------------------------
async def test_pause_and_resume(watch):
    assert (await call("pause_watch", watch_id=watch))["status"] == "paused"
    assert (await call("resume_watch", watch_id=watch))["status"] == "active"


@pytest.mark.parametrize(
    "tool",
    ["pause_watch", "resume_watch", "watch_history", "review_watch_removal", "check_now"],
)
async def test_a_watch_that_does_not_exist_says_so(tool):
    assert "no watch with id 99" in await refused(tool, watch_id=99)


async def test_removal_shows_what_goes_and_needs_a_yes(watch):
    review = await call("review_watch_removal", watch_id=watch)
    assert review["removed"] is False
    assert "DL197" in review["review"] and "can't be undone" in review["review"]
    assert open_store().get_watch(watch) is not None

    assert (await call("confirm_watch_removal", removal_id=review["removal_id"]))["removed"]
    assert open_store().get_watch(watch) is None
    assert "nothing changed" in await refused(
        "confirm_watch_removal", removal_id=review["removal_id"]
    )


async def test_removal_cannot_be_made_up(watch):
    assert "nothing changed" in await refused("confirm_watch_removal", removal_id="made-up")
    assert open_store().get_watch(watch) is not None


async def test_recipients_can_change_and_a_bad_address_says_why(watch):
    assert (await call("set_recipients", watch_id=watch, notify="ann@x.com"))["seat_alerts_to"] == [
        "ann@x.com"
    ]
    assert "not an email" in await refused("set_recipients", watch_id=watch, notify="nope")
    assert "no watch" in await refused("set_recipients", watch_id=99, notify="a@x.com")


# ---- settings -------------------------------------------------------------------------
async def test_daily_summary_on_and_off():
    assert (await call("alert_settings"))["daily_summary_on"] is True
    await call("set_daily_summary", on=False)
    assert open_store().get("digest_enabled") == "0"
    assert (await call("alert_settings"))["daily_summary_on"] is False


async def test_every_alert_kind_is_explained():
    answer = await call("alert_settings")
    assert {row["kind"] for row in answer["headlines"]} == {kind.value for kind in AlertKind}
    assert all(row["sent_when"] for row in answer["headlines"])


async def test_headline_changes_and_resets():
    answer = await call("set_alert_headline", kind="seats_open", text="  DEAL   FOUND ")
    assert answer["headline"] == "DEAL FOUND"
    assert mcp_server.headlines.load()[AlertKind.SEATS_OPEN] == "DEAL FOUND"
    assert (await call("set_alert_headline", kind="seats_open"))["headline"] == "SEATS OPEN"
    assert AlertKind.SEATS_OPEN not in mcp_server.headlines.load()


@pytest.mark.parametrize(
    ("arguments", "reason"),
    [
        ({"kind": "made_up", "text": "x"}, "made_up"),
        ({"kind": "seats_open", "text": "x" * 81}, "at most 80"),
        ({"kind": "seats_open", "text": "   "}, "can't be empty"),
    ],
)
async def test_a_bad_headline_says_why(arguments, reason):
    assert reason in await refused("set_alert_headline", **arguments)


async def test_automatic_checks_on_and_off(isolated):
    await call("set_automatic_checks", on=False)
    assert isolated.on is False
    assert (await call("alert_settings"))["automatic_checks_on"] is False
    await call("set_automatic_checks", on=True)
    assert isolated.on is True


async def test_automatic_checks_failure_says_why(monkeypatch):
    def fail():
        raise RuntimeError("access denied")

    monkeypatch.setattr(mcp_server.winsched, "install", fail)
    assert "access denied" in await refused("set_automatic_checks", on=True)


async def test_diagnose_lists_what_is_wrong(monkeypatch):
    monkeypatch.setattr(mail, "load_config", lambda: None)
    answer = await call("diagnose")
    rows = {row["check"]: row for row in answer["checks"]}
    assert rows["Email set up"]["ok"] is False
    assert rows["Email set up"]["fix"]
    assert rows["Scheduled task installed"]["ok"] is True
    assert rows["Scheduled task installed"]["fix"] is None
