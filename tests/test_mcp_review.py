"""Adding a watch through Claude: a review first, and nothing saved without a confirmation."""

from __future__ import annotations

import threading
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from mcp import Client

from jetbridge import mcp_server, service
from jetbridge.notify import email as mail
from jetbridge.service import open_store

FUTURE = date.today() + timedelta(days=200)
DETAILS = {
    "flight": "dl 197",
    "origin": "sea",
    "destination": "icn",
    "dates": FUTURE.isoformat(),
    "cabin": "business class",
    "program": "delta",
    "pax": 2,
}


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    config = mail.EmailConfig("o@x.com", "owner@x.com")
    monkeypatch.setattr(mail, "load_config", lambda: config)
    monkeypatch.setattr(mail.keyring, "get_password", lambda *_: "secret")
    monkeypatch.setattr(mcp_server.winsched, "is_installed", lambda: True)
    open_store().set(service.RISK_ACCEPTED, "1")
    mcp_server._checks.clear()


def watches():
    return open_store().list_watches()


async def call(client, tool, **arguments):
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result
    return result.structured_content


async def refused(client, tool, **arguments) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return result.content[0].text


async def test_adding_takes_the_two_review_tools_and_nothing_else_saves():
    async with Client(mcp_server.server) as client:
        names = {tool.name for tool in (await client.list_tools()).tools}
    assert {"review_watch", "confirm_watch"} <= names
    assert "add_watch" not in names


async def test_review_shows_the_details_and_saves_nothing():
    async with Client(mcp_server.server) as client:
        answer = await call(client, "review_watch", **DETAILS, notify="ann@example.com")
    assert answer["saved"] is False
    review = answer["review"]
    assert "DL197 from SEA to ICN" in review
    assert "business" in review and "Delta SkyMiles" in review
    assert "Passengers:  2" in review and FUTURE.strftime("%b %d, %Y") in review
    assert "Alerts go to: ann@example.com" in review
    assert watches() == []


async def test_review_says_the_owner_gets_alerts_when_nobody_is_named():
    async with Client(mcp_server.server) as client:
        answer = await call(client, "review_watch", **DETAILS)
    assert "Alerts go to: owner@x.com" in answer["review"]


async def test_confirm_saves_exactly_what_was_reviewed():
    async with Client(mcp_server.server) as client:
        answer = await call(client, "review_watch", **DETAILS, max_miles=120_000, notify="a@b.co")
        saved = await call(client, "confirm_watch", review_id=answer["review_id"])
    assert saved["flight"] == "DL197"
    [watch] = watches()
    assert (watch.flight.code, watch.origin, watch.destination) == ("DL197", "SEA", "ICN")
    assert (watch.cabin.value, watch.program, watch.pax) == ("business", "delta", 2)
    assert watch.dates == (FUTURE,)
    assert watch.max_miles == 120_000
    assert watch.recipients == ("a@b.co",)


async def test_a_review_saves_only_once():
    async with Client(mcp_server.server) as client:
        answer = await call(client, "review_watch", **DETAILS)
        await call(client, "confirm_watch", review_id=answer["review_id"])
        message = await refused(client, "confirm_watch", review_id=answer["review_id"])
    assert "nothing was saved" in message
    assert len(watches()) == 1


async def test_confirm_without_a_real_review_saves_nothing():
    async with Client(mcp_server.server) as client:
        message = await refused(client, "confirm_watch", review_id="made-up")
    assert "nothing was saved" in message
    assert watches() == []


async def test_a_changed_request_saves_only_the_review_it_confirms():
    async with Client(mcp_server.server) as client:
        first = await call(client, "review_watch", **DETAILS)
        second = await call(client, "review_watch", **{**DETAILS, "cabin": "economy"})
        await call(client, "confirm_watch", review_id=second["review_id"])
    assert first["review_id"] != second["review_id"]
    [watch] = watches()
    assert watch.cabin.value == "economy"


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"flight": "not a flight"}, "not a flight number"),
        ({"origin": "New York"}, "JFK, EWR, LGA"),
        ({"destination": "SEA"}, "are the same"),
        ({"dates": "5/6/2027"}, "write the month as a word"),
        ({"dates": "2020-01-01"}, "have passed"),
        ({"cabin": "luxury"}, "economy, premium, business or first"),
        ({"program": "lufthansa"}, "please choose"),
        ({"pax": 0}, "pax must be 1-9"),
        ({"notify": "not-an-address"}, "not-an-address"),
    ],
)
async def test_bad_details_get_no_review_and_say_why(change, reason):
    async with Client(mcp_server.server) as client:
        message = await refused(client, "review_watch", **{**DETAILS, **change})
    assert reason in message
    assert open_store().keys_with_prefix("chat_review:") == []
    assert watches() == []


@pytest.mark.parametrize("missing", ["cabin", "program", "pax"])
async def test_nothing_is_quietly_defaulted(missing):
    details = {key: value for key, value in DETAILS.items() if key != missing}
    async with Client(mcp_server.server) as client:
        await refused(client, "review_watch", **details)
    assert open_store().keys_with_prefix("chat_review:") == []


class FakeReport:
    checks = ()
    alerts_sent = 0
    stopped_early = None


@pytest.fixture
def saved_watch():
    return service.create_watch(
        open_store(),
        flight="DL197",
        origin="SEA",
        destination="ICN",
        dates=FUTURE.isoformat(),
        cabin="economy",
        program="delta",
    ).id


async def test_a_quick_check_answers_straight_away(saved_watch, monkeypatch):
    monkeypatch.setattr(mcp_server.service, "live_tick", lambda *_, **__: FakeReport())
    async with Client(mcp_server.server) as client:
        answer = await call(client, "check_now", watch_id=saved_watch)
    assert answer["finished"] is True and answer["results"] == []
    assert mcp_server._checks == {}


async def test_a_date_checked_twice_reports_only_the_latest_answer(saved_watch, monkeypatch):
    def line(status):
        result = SimpleNamespace(
            status=SimpleNamespace(value=status), miles=97_000, seats_text="1", detail=None
        )
        return SimpleNamespace(day=FUTURE, result=result)

    report = SimpleNamespace(
        checks=[line("available"), line("not_offered")], alerts_sent=1, stopped_early=None
    )
    monkeypatch.setattr(mcp_server.service, "live_tick", lambda *_, **__: report)
    async with Client(mcp_server.server) as client:
        answer = await call(client, "check_now", watch_id=saved_watch)
    assert [row["status"] for row in answer["results"]] == ["not_offered"]


async def test_a_slow_check_comes_back_in_time_and_can_be_collected(saved_watch, monkeypatch):
    release = threading.Event()
    calls = []

    def slow(*_, **__):
        calls.append(1)
        release.wait(10)
        return FakeReport()

    monkeypatch.setattr(mcp_server.service, "live_tick", slow)
    monkeypatch.setattr(mcp_server, "CHECK_WAIT_S", 0.05)
    async with Client(mcp_server.server) as client:
        first = await call(client, "check_now", watch_id=saved_watch)
        assert first["finished"] is False
        # Asking again must not start a second search on the airline's site.
        again = await call(client, "check_now", watch_id=saved_watch)
        assert again["check_id"] == first["check_id"]
        waiting = await call(client, "check_result", check_id=first["check_id"])
        assert waiting["finished"] is False
        release.set()
        monkeypatch.setattr(mcp_server, "CHECK_WAIT_S", 5)
        done = await call(client, "check_result", check_id=first["check_id"])
    assert done["finished"] is True and done["results"] == []
    assert calls == [1]


async def test_a_check_failure_says_why(saved_watch, monkeypatch):
    def busy(*_, **__):
        raise service.TickBusy("another jetbridge check is already running")

    monkeypatch.setattr(mcp_server.service, "live_tick", busy)
    async with Client(mcp_server.server) as client:
        message = await refused(client, "check_now", watch_id=saved_watch)
    assert "already running" in message
    assert mcp_server._checks == {}


async def test_checks_need_a_real_watch_and_a_real_check_id():
    async with Client(mcp_server.server) as client:
        assert "no watch" in await refused(client, "check_now", watch_id=999)
        assert "nothing is running" in await refused(client, "check_result", check_id="made-up")
