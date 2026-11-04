"""Watching a flight you've already booked, for a lower price."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import DAY, add_cx, answer_from, cx_slice, make_runner, response
from mcp import Client

from jetbridge import board, draft, mcp_server, service
from jetbridge.alerts import AlertKind, DateState, render_availability
from jetbridge.matcher import evaluate
from jetbridge.models import (
    AnswerKind,
    Cabin,
    CheckResult,
    CheckStatus,
    Flight,
    SearchAnswer,
    Watch,
)
from jetbridge.notify import email as mail
from jetbridge.runner import TickReport

FUTURE = (date.today() + timedelta(days=60)).isoformat()
WATCH = Watch(1, Flight("CX", "805"), "JFK", "HKG", (DAY,), Cabin.BUSINESS, booked_miles=100_000)


def priced(miles: int):
    return answer_from(response(cx_slice(business_miles=miles, seats=2)))


@pytest.mark.parametrize(
    ("miles", "status"),
    [
        (99_999, CheckStatus.AVAILABLE),
        (100_000, CheckStatus.NOT_OFFERED),
        (120_000, CheckStatus.NOT_OFFERED),
    ],
)
def test_only_a_lower_price_counts(miles, status):
    result = evaluate(WATCH, DAY, priced(miles))
    assert result.status is status
    if status is CheckStatus.NOT_OFFERED:
        assert result.miles == miles and "not below the 100,000 you paid" in result.detail


def test_an_unclear_answer_never_counts():
    answer = SearchAnswer(AnswerKind.DRIFT, detail="page changed")
    assert evaluate(WATCH, DAY, answer).status is CheckStatus.UNKNOWN


async def test_it_alerts_only_when_cheaper_and_after_the_re_check(store, clock):
    add_cx(store, clock, booked_miles=100_000)
    runner, _, sender, _ = make_runner(store, clock, [priced(100_000)])
    await runner.tick()
    assert not [s for s in sender.subjects if "CHEAPER" in s]

    store.set_next_check(store.list_watches("active")[0].id, clock())
    runner, fetcher, sender, _ = make_runner(store, clock, [priced(80_000), priced(80_000)])
    clock.now += 86_400
    await runner.tick()
    assert len(fetcher.queries) == 2
    subject, body = next((s, b) for s, b in sender.sent if "CHEAPER THAN YOU PAID" in s)
    assert "80,000" in subject
    assert "You paid: 100,000 miles per person, so this saves 20,000" in body
    assert "book the cheaper seat before cancelling" in body


async def test_no_alert_when_the_lower_price_is_gone_on_the_re_check(store, clock):
    add_cx(store, clock, booked_miles=100_000)
    runner, _, sender, _ = make_runner(store, clock, [priced(80_000), priced(100_000)])
    await runner.tick()
    assert not [s for s in sender.subjects if "CHEAPER" in s]


def test_a_custom_headline_cannot_hide_what_it_means():
    result = CheckResult(CheckStatus.AVAILABLE, miles=80_000, seats_exact=2)
    subject, _ = render_availability(
        AlertKind.SEATS_OPEN,
        WATCH,
        DAY,
        result,
        DateState(),
        1_800_000_000.0,
        "https://example.com",
        {AlertKind.SEATS_OPEN: "DEAL FOUND"},
    )
    assert subject.startswith("CHEAPER THAN YOU PAID")


def test_when_it_goes_the_email_says_the_booking_is_fine():
    _, body = render_availability(
        AlertKind.SEATS_GONE,
        WATCH,
        DAY,
        CheckResult(CheckStatus.NOT_OFFERED),
        DateState(miles=80_000),
        1_800_000_000.0,
        "https://example.com",
    )
    assert "Your booking isn't affected." in body


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"flight": "any"}, "one exact flight"),
        ({"dates": f"{FUTURE},{(date.today() + timedelta(days=61)).isoformat()}"}, "one date"),
        ({"max_miles": 90_000}, "already alerts only below"),
        ({"booked_miles": 0}, "must be positive"),
    ],
)
def test_it_has_to_be_one_flight_on_one_date(fields, problem):
    base = {
        "flight": "CX805",
        "origin": "JFK",
        "destination": "HKG",
        "dates": FUTURE,
        "cabin": "business",
        "booked_miles": 100_000,
    }
    with pytest.raises(ValueError, match=problem):
        service.validate_watch(**{**base, **fields})


def test_it_is_saved_and_survives_an_edit(store):
    watch = service.create_watch(
        store,
        flight="CX805",
        origin="JFK",
        destination="HKG",
        dates=FUTURE,
        cabin="business",
        booked_miles=100_000,
    )
    assert store.get_watch(watch.id).booked_miles == 100_000
    changed = service.change_watch(store, watch, dates=FUTURE, checks_per_day=4, max_miles=None)
    assert changed.booked_miles == 100_000 and changed.price_limit == 99_999


def test_the_review_says_what_was_paid():
    text = draft.review_text(
        draft.WatchDraft("JFK", "HKG", FUTURE, "CX805", booked_miles=100_000), "me@x.com"
    )
    assert "you paid 100,000 miles per person, so only alert below that" in text


@pytest.fixture
def chat(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    monkeypatch.setattr(service, "live_tick", lambda *_, **__: TickReport())
    monkeypatch.setattr(mail, "load_config", lambda: mail.EmailConfig("o@x.com", "me@x.com"))
    monkeypatch.setattr(mail.keyring, "get_password", lambda *_: "secret")
    monkeypatch.setattr(mcp_server.autorun, "is_installed", lambda: True)
    mcp_server._checks.clear()
    service.open_store().set(service.RISK_ACCEPTED, "1")


@pytest.mark.parametrize(
    ("fields", "field", "value"),
    [
        ({"flight": "CX805", "booked_miles": 100_000}, "booked_miles", 100_000),
        ({"flight": "any", "pax": 2, "whole_party": 4}, "whole_party", 4),
    ],
)
async def test_chat_saves_it(chat, fields, field, value):
    details = {
        "origin": "JFK",
        "destination": "HKG",
        "dates": FUTURE,
        "cabin": "business",
        "program": "american",
        "pax": 1,
    }
    async with Client(mcp_server.server) as client:
        review = await client.call_tool("review_watch", {**details, **fields})
        await client.call_tool(
            "confirm_watch", {"review_id": review.structured_content["review_id"]}
        )
    assert getattr(service.open_store().list_watches()[0], field) == value


async def test_chat_review_keeps_it_and_refuses_several(chat):
    async with Client(mcp_server.server) as client:
        shared = {
            "origin": "JFK",
            "destination": "HKG",
            "dates": FUTURE,
            "cabin": "business",
            "program": "american",
            "pax": 1,
            "booked_miles": 100_000,
        }
        result = await client.call_tool("review_watch", {"flight": "CX805", **shared})
        assert "you paid 100,000" in result.structured_content["review"]
        refused = await client.call_tool("review_watch", {"flight": "CX805 or CX889", **shared})
        assert refused.is_error and "one flight in one cabin" in str(refused.content)


def test_a_normal_watch_is_unchanged():
    plain = Watch(1, Flight("CX", "805"), "JFK", "HKG", (DAY,), Cabin.BUSINESS, max_miles=90_000)
    assert plain.price_limit == 90_000


async def test_the_summary_says_what_was_paid(store, clock):
    add_cx(store, clock, booked_miles=100_000)
    runner, _, _, _ = make_runner(store, clock, [priced(120_000)])
    await runner.tick()
    _, body = runner.build_digest(store.list_watches("active"))
    assert "Booked at: 100,000 miles per person" in body


def test_the_board_shows_what_was_paid():
    assert "you paid" in board.PAGE and "watch.booked_at_miles" in board.PAGE
