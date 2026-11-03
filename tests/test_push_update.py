"""Phone alerts, the booking link coming first, and staying up to date."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from conftest import FakeSender, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge import doctor, platforms, updater
from jetbridge.alerts import AlertKind
from jetbridge.notify import push
from jetbridge.notify.email import flush_outbox
from jetbridge.store import NewAlert

CLOSED = response(cx_slice(business_miles=None))
OPEN = response(cx_slice(seats=2))
URL = "https://www.aa.com/x"


@pytest.fixture
def posted(monkeypatch):
    sent: list[dict] = []
    monkeypatch.setattr(push, "_post", sent.append)
    return sent


def _queue_push(store, clock, subject="SEATS OPEN: CX805"):
    store.enqueue(push.alert_for(AlertKind.SEATS_OPEN, subject, URL), clock())


# ---- turning it on --------------------------------------------------------------------
def test_phone_alerts_start_off():
    assert not push.enabled() and not push.wanted_for(AlertKind.SEATS_OPEN)


def test_the_channel_name_is_long_random_and_kept():
    first = push.turn_on()
    assert first.startswith("jetbridge-") and len(first) >= 40
    assert push.turn_on() == first  # a subscribed phone keeps working
    push.turn_off()
    assert push.turn_on() != first


def test_a_damaged_file_means_off():
    push.turn_on()
    push._path().write_text("{not json", encoding="utf-8")
    assert not push.enabled()


@pytest.mark.parametrize(
    ("kind", "wanted"),
    [
        (AlertKind.SEATS_OPEN, True),
        (AlertKind.SEATS_OPEN_UNCONFIRMED, True),
        (AlertKind.PRICE_DROP, True),
        (AlertKind.MORE_SEATS, False),
        (AlertKind.REMINDER, False),
        (AlertKind.SEATS_GONE, False),
    ],
)
def test_only_alerts_worth_a_buzz_go_to_the_phone(kind, wanted):
    push.turn_on()
    assert push.wanted_for(kind) is wanted


# ---- sending --------------------------------------------------------------------------
def test_a_phone_alert_carries_the_link_and_channel(store, clock, posted):
    name = push.turn_on()
    _queue_push(store, clock)
    assert flush_outbox(store, FakeSender(), clock()) == 0  # counts emails only
    assert posted[0]["topic"] == name and posted[0]["click"] == URL
    assert posted[0]["title"] == "SEATS OPEN: CX805"
    assert store.pending_alerts() == []


def test_a_phone_alert_never_goes_as_an_email(store, clock, posted):
    push.turn_on()
    _queue_push(store, clock)
    sender = FakeSender()
    flush_outbox(store, sender, clock())
    assert sender.sent == []


def test_a_failed_phone_alert_does_not_hold_up_email(store, clock, monkeypatch):
    def down(_):
        raise OSError("ntfy is down")

    monkeypatch.setattr(push, "_post", down)
    push.turn_on()
    _queue_push(store, clock)
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "body"), clock())
    sender = FakeSender()
    assert flush_outbox(store, sender, clock()) == 1
    assert sender.subjects == ["SEATS OPEN"]
    assert store.get(push.LAST_ERROR) == "OSError: ntfy is down"


def test_a_failed_email_does_not_hold_up_the_phone(store, clock, posted):
    push.turn_on()
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "body"), clock())
    _queue_push(store, clock)
    sender = FakeSender()
    sender.failing = True
    flush_outbox(store, sender, clock())
    assert len(posted) == 1
    assert [item.subject for item in store.pending_alerts()] == ["SEATS OPEN"]


def test_a_stale_phone_alert_is_dropped_but_email_stays(store, clock, posted):
    push.turn_on()
    _queue_push(store, clock)
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "body"), clock())
    sender = FakeSender()
    sender.failing = True
    flush_outbox(store, sender, clock() + push.MAX_AGE_S + 1)
    assert posted == []
    assert [item.recipient for item in store.pending_alerts()] == [None]


def test_turning_it_off_with_one_queued_does_not_crash(store, clock):
    push.turn_on()
    _queue_push(store, clock)
    push.turn_off()
    assert flush_outbox(store, FakeSender(), clock()) == 0


async def test_a_real_opening_buzzes_once_and_still_emails_everyone(store, clock, posted):
    push.turn_on()
    add_cx(store, clock, recipients=("ann@x.com", "bo@y.com"))
    answers = [answer_from(CLOSED), answer_from(OPEN), answer_from(OPEN)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert len(posted) == 1 and posted[0]["title"].startswith("SEATS OPEN: CX805")
    assert "aa.com" in posted[0]["click"]
    assert sorted(sender.to) == ["ann@x.com", "bo@y.com"]


async def test_no_buzz_when_phone_alerts_are_off(store, clock, posted):
    add_cx(store, clock)
    answers = [answer_from(CLOSED), answer_from(OPEN), answer_from(OPEN)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    assert posted == [] and len(sender.sent) == 1


async def test_unknown_never_buzzes(store, clock, posted):
    from jetbridge.models import AnswerKind, SearchAnswer

    push.turn_on()
    add_cx(store, clock)
    runner, _, _, _ = make_runner(store, clock, [SearchAnswer(AnswerKind.ERROR, detail="x")])
    await runner.tick()
    assert posted == []


def test_doctor_keeps_phone_and_email_trouble_apart(store, clock, monkeypatch):
    def down(_):
        raise OSError("ntfy is down")

    monkeypatch.setattr(push, "_post", down)
    push.turn_on()
    _queue_push(store, clock)
    flush_outbox(store, FakeSender(), clock())
    checks = {check.label: check for check in doctor.checks(store)}
    assert checks["Emails sending"].ok
    assert not checks["Phone alerts sending (optional)"].ok
    assert not checks["Phone alerts sending (optional)"].serious


# ---- the link comes first -------------------------------------------------------------
async def test_the_booking_link_is_at_the_top_of_a_seat_email(store, clock):
    add_cx(store, clock)
    answers = [answer_from(CLOSED), answer_from(OPEN), answer_from(OPEN)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now = store.get_watch(1).next_check_at + 1
    await runner.tick()
    lines = sender.sent[0][1].splitlines()
    assert lines[0] == "Check and book:" and lines[1].startswith("https://www.aa.com/")
    assert sum(line.startswith("https://") for line in lines) == 1


# ---- noticing a new version -----------------------------------------------------------
@pytest.mark.parametrize(
    ("latest", "installed", "newer"),
    [
        ("0.5.0", "0.4.0", "0.5.0"),
        ("0.10.0", "0.9.0", "0.10.0"),  # numbers, not text
        ("0.4.0", "0.4.0", None),
        ("0.3.0", "0.4.0", None),
        ("garbage", "0.4.0", None),
    ],
)
def test_only_a_real_newer_version_counts(store, latest, installed, newer):
    store.set("latest_version", latest)
    assert updater.newer_version(store, installed) == newer


def test_reads_the_version_from_the_project_file():
    assert updater.version_in('[project]\nname = "x"\nversion = "1.2.3"\n') == "1.2.3"
    assert updater.version_in('python_version = "3.12"\n') is None
    assert updater.version_in("nothing here") is None


def test_it_looks_at_most_once_a_day(store, monkeypatch):
    calls = []
    monkeypatch.setattr(updater, "_fetch_latest", lambda: calls.append(1) or "9.0.0")
    updater.refresh(store, 2e9)
    updater.refresh(store, 2e9 + 1000)
    assert len(calls) == 1 and store.get("latest_version") == "9.0.0"
    updater.refresh(store, 2e9 + 86400)
    assert len(calls) == 2


def test_being_offline_changes_nothing(store, monkeypatch):
    def offline():
        raise OSError("no network")

    monkeypatch.setattr(updater, "_fetch_latest", offline)
    updater.refresh(store, 2e9)
    assert store.get("latest_version") is None


def test_the_check_can_be_turned_off(store, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_NO_UPDATE_CHECK", "1")
    monkeypatch.setattr(updater, "_fetch_latest", lambda: pytest.fail("looked"))
    updater.refresh(store, 2e9)


async def test_the_daily_summary_mentions_a_new_version(store, clock):
    store.set("digest_enabled", "1")
    store.set("latest_version", "99.0.0")
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    assert "jetbridge 99.0.0 is available" in sender.sent[0][1]


# ---- installing it --------------------------------------------------------------------
def _done(stdout="", stderr="", code=0):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def machine(monkeypatch, tmp_path):
    """A checkout with git and uv available, and every command recorded rather than run."""
    calls: list[tuple[str, ...]] = []
    answers: dict[str, subprocess.CompletedProcess] = {}

    def run(*args, cwd):
        calls.append(args)
        return answers.get(args[1], _done())

    monkeypatch.setattr(updater, "_run", run)
    monkeypatch.setattr(updater, "project_folder", lambda: Path("C:/jb"))
    monkeypatch.setattr(updater.shutil, "which", lambda name: name)
    monkeypatch.setattr(updater, "current", lambda: "0.4.0")
    monkeypatch.setattr(updater.autorun, "is_installed", lambda: False)
    monkeypatch.setattr(
        updater.claude_desktop, "status", lambda: updater.claude_desktop.State.CONNECTED
    )
    return calls, answers


def test_an_update_only_fast_forwards_then_syncs(machine):
    calls, _ = machine
    report = updater.install()
    assert calls == [
        ("git", "status", "--porcelain", "--untracked-files=no"),
        ("git", "pull", "--ff-only"),
        ("uv", "sync"),
    ]
    assert any("Restart Claude Desktop" in line for line in report)


def test_local_changes_stop_the_update_before_anything_changes(machine):
    calls, answers = machine
    answers["status"] = _done(stdout=" M src/x.py\n")
    with pytest.raises(updater.UpdateProblem, match="local changes"):
        updater.install()
    assert len(calls) == 1


def test_a_pull_that_cant_fast_forward_stops(machine):
    calls, answers = machine
    answers["pull"] = _done(stderr="not possible to fast-forward", code=1)
    with pytest.raises(updater.UpdateProblem, match="fast-forward"):
        updater.install()
    assert ("uv", "sync") not in calls


def test_a_locked_program_says_what_to_do(machine):
    _, answers = machine
    answers["sync"] = _done(stderr="failed to remove file: os error 32", code=1)
    with pytest.raises(updater.UpdateProblem, match="Quit Claude Desktop"):
        updater.install()


def test_an_old_claude_connection_is_refreshed(machine, monkeypatch):
    done = []
    monkeypatch.setattr(
        updater.claude_desktop, "status", lambda: updater.claude_desktop.State.OUT_OF_DATE
    )
    monkeypatch.setattr(updater.claude_desktop, "connect", lambda: done.append(1))
    updater.install()
    assert done == [1]


def test_a_copy_without_git_says_so(monkeypatch):
    monkeypatch.setattr(updater, "project_folder", lambda: None)
    with pytest.raises(updater.UpdateProblem, match="git"):
        updater.install()


def test_the_launcher_runs_through_python():
    assert b"-m jetbridge" in platforms.launcher_bytes()


def test_the_alert_body_is_kept_as_data():
    alert = push.alert_for(AlertKind.SEATS_OPEN, "s", URL)
    assert json.loads(alert.body)["click"] == URL and alert.recipient == push.PUSH_RECIPIENT
