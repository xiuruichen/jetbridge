"""Watching one route across every program, and comparing what's open between them."""

from __future__ import annotations

import sqlite3

import pytest
from conftest import DAY, answer_from, cx_slice, make_runner, response
from typer.testing import CliRunner

from jetbridge import service
from jetbridge.acquire.sites import parse_programs
from jetbridge.alerts import DateState
from jetbridge.cli import app
from jetbridge.models import Cabin, CheckResult, CheckStatus, Flight
from jetbridge.store import Store


def test_programs_parse_from_words():
    assert parse_programs("all") == ("american", "delta", "united")
    assert parse_programs("american, delta and united") == ("american", "delta", "united")
    assert parse_programs("Delta") == ("delta",)
    assert parse_programs("delta, delta") == ("delta",)
    with pytest.raises(ValueError, match="please choose"):
        parse_programs("southwest")


def _group(store, clock, programs=("american", "delta")):
    watches = [
        store.add_watch(
            Flight("ANY", ""), "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock(), program=p
        )
        for p in programs
    ]
    store.set_group([w.id for w in watches], watches[0].id)
    return [store.get_watch(w.id) for w in watches]


def _open(store, watch, miles, at):
    store.commit_check(
        watch.id,
        DAY,
        CheckResult(CheckStatus.AVAILABLE, miles=miles),
        DateState(status=CheckStatus.AVAILABLE, miles=miles, last_conclusive_at=at),
        [],
        at,
    )


def test_siblings_are_the_other_active_watches_in_the_group(store, clock):
    first, second = _group(store, clock)
    assert [w.id for w in store.group_siblings(first)] == [second.id]
    store.set_watch_status(second.id, "paused")
    assert store.group_siblings(first) == []
    lone = store.add_watch(Flight("CX", "805"), "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock())
    assert store.group_siblings(lone) == []


def test_an_alert_says_what_else_is_open_on_other_programs(store, clock):
    runner, *_ = make_runner(store, clock, [])
    american, delta = _group(store, clock)
    assert runner._also_open(american, DAY) is None  # nothing open elsewhere yet
    _open(store, delta, 70_000, clock())
    text = runner._also_open(american, DAY)
    assert "Delta SkyMiles 70,000 miles" in text and "Compare before you move" in text
    assert runner._also_open(delta, DAY) is None  # American isn't open


def test_a_different_route_or_cabin_is_not_compared(store, clock):
    runner, *_ = make_runner(store, clock, [])
    american, _ = _group(store, clock)
    other = store.add_watch(
        Flight("ANY", ""), "JFK", "ICN", (DAY,), Cabin.BUSINESS, now=clock(), program="united"
    )
    store.set_group([other.id], american.group_id)
    _open(store, store.get_watch(other.id), 50_000, clock())
    assert runner._also_open(american, DAY) is None


async def test_the_compare_line_reaches_the_email(store, clock):
    _group(store, clock)
    open_answer = answer_from(response(cx_slice(business_miles=80_000)))
    runner, _, sender, _ = make_runner(store, clock, [open_answer, open_answer, open_answer])
    american, delta = store.list_watches()
    _open(store, delta, 70_000, clock())
    await runner.tick(only_watch=american.id)
    body = next(b for s, b in sender.sent if s.startswith("SEATS OPEN"))
    assert "Also open: Delta SkyMiles 70,000 miles" in body


def test_an_old_database_gains_the_new_columns(tmp_path):
    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE watches (id INTEGER PRIMARY KEY AUTOINCREMENT, carrier TEXT NOT NULL,"
        " number TEXT NOT NULL, origin TEXT NOT NULL, destination TEXT NOT NULL,"
        " dates TEXT NOT NULL, cabin TEXT NOT NULL, program TEXT NOT NULL, pax INTEGER NOT NULL,"
        " checks_per_day INTEGER NOT NULL, max_miles INTEGER, status TEXT NOT NULL,"
        " created_at REAL NOT NULL, next_check_at REAL NOT NULL)"
    )
    old.execute(
        "INSERT INTO watches VALUES (1,'CX','805','JFK','HKG','[\"2027-05-21\"]','business',"
        "'american',1,6,NULL,'active',0,0)"
    )
    old.commit()
    old.close()
    watch = Store(path).get_watch(1)
    assert watch.group_id is None and watch.flex_days == 0 and watch.flex_origins == ()


def test_the_command_saves_a_grouped_route_watch(store, monkeypatch):
    monkeypatch.setattr(service, "open_store", lambda: store)
    result = CliRunner().invoke(
        app,
        [
            "watch", "add", "any", "--from", "JFK", "--to", "HKG", "--dates", "2027-05-21",
            "--program", "american,delta", "--origins", "north-america", "--flex-days", "1",
            "--notify", "me", "--yes",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    watches = store.list_watches()
    assert [w.program for w in watches] == ["american", "delta"]
    assert watches[0].group_id == watches[1].group_id == watches[0].id
    assert watches[0].flex_days == 1 and "YYZ" in watches[0].flex_origins
