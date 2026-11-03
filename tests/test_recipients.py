"""Sending a watch's seat alerts to other people from one computer."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import DAY, FakeSender, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge.models import AnswerKind, Cabin, Flight, SearchAnswer
from jetbridge.notify.email import EmailConfig, build_message, flush_outbox
from jetbridge.service import create_watch, parse_recipients
from jetbridge.store import NewAlert, Store

OPEN = response(cx_slice(seats=2))
CLOSED = response(cx_slice(business_miles=None))
ANN, BO = "ann@example.com", "bo@example.org"
CONFIG = EmailConfig("owner@gmail.com", "owner@gmail.com")


# ---- parsing --------------------------------------------------------------------------
def test_parse_separators_case_and_duplicates():
    assert parse_recipients("Ann@Example.com, bo@example.org;ann@example.com") == (ANN, BO)
    assert parse_recipients("ann@example.com bo@example.org") == (ANN, BO)


@pytest.mark.parametrize("text", [None, "", "  ", "me", "ME"])
def test_empty_or_me_means_owner(text):
    assert parse_recipients(text) == ()


@pytest.mark.parametrize("bad", ["ann", "ann@", "@x.com", "ann@x", "a@b@c.com", "ann@x..com"])
def test_bad_addresses_are_rejected(bad):
    with pytest.raises(ValueError):
        parse_recipients(bad)


def test_too_many_recipients_rejected():
    with pytest.raises(ValueError):
        parse_recipients(",".join(f"p{i}@x.com" for i in range(11)))


def test_create_watch_saves_recipients(store, clock):
    watch = create_watch(
        store,
        flight="CX805",
        origin="YYZ",
        destination="HKG",
        dates="2027-05-21",
        cabin="business",
        recipients=f"{ANN},{BO}",
        now=clock(),
    )
    assert store.get_watch(watch.id).recipients == (ANN, BO)


# ---- delivery -------------------------------------------------------------------------
async def test_seat_alert_goes_only_to_each_recipient(store, clock):
    add_cx(store, clock, recipients=(ANN, BO))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN)])
    await runner.tick()
    assert sender.to == [ANN, BO]
    assert all(s.startswith("SEATS OPEN") for s in sender.subjects)


async def test_no_recipients_goes_to_owner(store, clock):
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN)])
    await runner.tick()
    assert sender.to == [None]


async def test_each_watch_alerts_its_own_people_from_one_search(store, clock):
    add_cx(store, clock, recipients=(ANN,))
    add_cx(store, clock, recipients=(BO,))
    runner, fetcher, sender, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN)])
    await runner.tick()
    assert len(fetcher.queries) == 2, "one search plus one shared re-check"
    assert sorted(sender.to) == [ANN, BO]


async def test_seats_gone_also_reaches_recipients(store, clock):
    add_cx(store, clock, recipients=(ANN,))
    answers = [answer_from(OPEN), answer_from(OPEN), answer_from(CLOSED)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now += 86400
    await runner.tick()
    assert sender.to == [ANN, ANN] and sender.subjects[1].startswith("Seats gone")


async def test_health_warnings_go_only_to_owner(store, clock):
    add_cx(store, clock, recipients=(ANN,))
    errors = [SearchAnswer(AnswerKind.ERROR, detail="network") for _ in range(6)]
    runner, _, sender, _ = make_runner(store, clock, errors)
    for _ in range(6):
        clock.now += 86400 / 3
        await runner.tick()
    assert any("can't see" in s for s in sender.subjects)
    assert set(sender.to) == {None}


def _digests(sender):
    return {
        to: body
        for to, (subject, body) in zip(sender.to, sender.sent, strict=True)
        if subject.startswith("jetbridge daily")
    }


async def test_digest_goes_to_everyone_without_showing_other_addresses(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock, recipients=(ANN, BO))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    bodies = _digests(sender)
    assert set(bodies) == {None, ANN, BO}
    assert ANN in bodies[None] and BO in bodies[None]
    for person in (ANN, BO):
        assert ANN not in bodies[person] and BO not in bodies[person]
        assert "CX805" in bodies[person] and "the person who added you" in bodies[person]
    sent_so_far = len(sender.to)
    await runner.tick()
    assert len(sender.to) == sent_so_far  # once a day each


async def test_digest_keeps_health_notes_for_the_owner(store, clock):
    store.set("digest_enabled", "1")
    store.set("longest_gap_s", str(7 * 3600))
    add_cx(store, clock, recipients=(ANN,))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    bodies = _digests(sender)
    assert "off or asleep" in bodies[None] and "off or asleep" not in bodies[ANN]
    assert "(watch " in bodies[None] and "(watch " not in bodies[ANN]


def test_someones_digest_covers_only_their_watches(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock, recipients=(ANN,))
    store.add_watch(
        Flight("CX", "889"), "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock(), recipients=(BO,)
    )
    runner, _, _, _ = make_runner(store, clock, [])
    runner._digest()
    bodies = {item.recipient: item.body for item in store.pending_alerts()}
    assert "CX805" in bodies[ANN] and "CX889" not in bodies[ANN]
    assert "CX889" in bodies[BO] and "CX805" not in bodies[BO]
    assert "CX805" in bodies[None] and "CX889" in bodies[None]


async def test_owner_on_the_list_gets_one_digest(store, clock, monkeypatch):
    monkeypatch.setattr("jetbridge.runner.owner_address", lambda: "Me@example.com")
    store.set("digest_enabled", "1")
    add_cx(store, clock, recipients=("me@example.com", ANN))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    assert set(_digests(sender)) == {None, ANN}


def test_refused_address_does_not_block_others_and_owner_is_told(store, clock):
    sender = FakeSender()
    sender.refused = {ANN}
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "details", recipient=ANN), clock())
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "details", recipient=BO), clock())
    flush_outbox(store, sender, clock())
    assert sender.to == [BO]
    flush_outbox(store, sender, clock())
    assert sender.to == [BO, None]
    assert ANN in sender.subjects[1] and "details" in sender.sent[1][1]
    assert not store.pending_alerts()


def test_outage_keeps_every_recipients_alert(store, clock):
    sender = FakeSender()
    sender.failing = True
    store.enqueue(NewAlert("seats_open", "S", "b", recipient=ANN), clock())
    store.enqueue(NewAlert("seats_open", "S", "b", recipient=BO), clock())
    flush_outbox(store, sender, clock())
    assert len(store.pending_alerts()) == 2
    sender.failing = False
    flush_outbox(store, sender, clock())
    assert sender.to == [ANN, BO]


# ---- message --------------------------------------------------------------------------
def test_other_people_see_one_address_and_an_explanation():
    message = build_message(CONFIG, "S", "details", ANN)
    assert message["To"] == ANN and message["Cc"] is None and message["Bcc"] is None
    text = message.get_content()
    assert "details" in text and "someone added your address" in text


@pytest.mark.parametrize("to", [None, CONFIG.to_address])
def test_owner_gets_no_footer(to):
    message = build_message(CONFIG, "S", "details", to)
    assert message["To"] == CONFIG.to_address
    assert "someone added" not in message.get_content()


# ---- upgrading ------------------------------------------------------------------------
def test_database_from_earlier_version_is_upgraded(tmp_path, clock):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(
        """
        CREATE TABLE watches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, carrier TEXT NOT NULL, number TEXT NOT NULL,
          origin TEXT NOT NULL, destination TEXT NOT NULL, dates TEXT NOT NULL,
          cabin TEXT NOT NULL, program TEXT NOT NULL, pax INTEGER NOT NULL,
          checks_per_day INTEGER NOT NULL, max_miles INTEGER, status TEXT NOT NULL,
          created_at REAL NOT NULL, next_check_at REAL NOT NULL);
        INSERT INTO watches VALUES
          (1,'CX','805','YYZ','HKG','["2027-05-21"]','business','american',1,6,NULL,'active',0,0);
        CREATE TABLE outbox (
          id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, dedupe_key TEXT UNIQUE,
          subject TEXT NOT NULL, body TEXT NOT NULL, created_at REAL NOT NULL, sent_at REAL,
          attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT);
        INSERT INTO outbox (kind, subject, body, created_at) VALUES ('digest','S','b',0);
        """
    )
    old.commit()
    old.close()

    store = Store(path)
    watch = store.get_watch(1)
    assert watch.recipients == () and watch.dates == (DAY,)
    assert store.pending_alerts()[0].recipient is None
    store.set_recipients(1, (ANN,))
    assert store.get_watch(1).recipients == (ANN,)
    store.close()
    Store(path).close()  # upgrading twice is harmless
