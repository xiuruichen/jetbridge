"""Custom alert headlines: the wording changes, and a bad file can't cost anyone an alert."""

from __future__ import annotations

import pytest
from conftest import add_cx, answer_from, cx_slice, make_runner, response
from typer.testing import CliRunner

from jetbridge import headlines
from jetbridge.alerts import AlertKind
from jetbridge.cli import app
from jetbridge.paths import alerts_path

CLOSED = response(cx_slice(business_miles=None))
OPEN = response(cx_slice(seats=2))
cli = CliRunner()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))


async def _open_alert_subject(store, clock, custom):
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(
        store, clock, [answer_from(CLOSED), answer_from(OPEN), answer_from(OPEN)]
    )
    runner.headlines = custom
    await runner.tick()
    clock.now += 86400 / 6 * 1.5
    await runner.tick()
    return sender.sent[0]


async def test_custom_headline_changes_only_the_wording(store, clock):
    subject, body = await _open_alert_subject(
        store, clock, {AlertKind.SEATS_OPEN: "DEAL FOUND, BOOK NOW"}
    )
    assert subject == "DEAL FOUND, BOOK NOW: CX805 business on May 21, 2027 - 110,000 mi"
    assert "110,000 miles + USD 5.60 per person" in body


async def test_no_custom_headlines_keeps_the_builtin_wording(store, clock):
    subject, _ = await _open_alert_subject(store, clock, headlines.load())
    assert subject.startswith("SEATS OPEN: ")


@pytest.mark.parametrize(
    "content",
    ["{not json", "[]", '{"headlines": {"nope": "x"}}', '{"headlines": {"seats_open": ""}}'],
)
def test_bad_file_falls_back_and_is_reported(content):
    alerts_path().write_text(content, encoding="utf-8")
    assert headlines.load() == {}
    assert headlines.problem() is not None


@pytest.mark.parametrize("text", ["", "   ", "x" * 81, "bell\x07"])
def test_bad_headlines_are_refused(text):
    with pytest.raises(ValueError):
        headlines.clean(text)


def test_line_breaks_cannot_reach_the_subject():
    assert headlines.clean("BOOK\r\nBcc: someone@example.com") == "BOOK Bcc: someone@example.com"


def test_command_sets_lists_and_resets():
    result = cli.invoke(app, ["notify", "headline", "seats_open", "DEAL FOUND, BOOK NOW"])
    assert result.exit_code == 0, result.output
    assert headlines.load() == {AlertKind.SEATS_OPEN: "DEAL FOUND, BOOK NOW"}

    listing = cli.invoke(app, ["notify", "headline"]).output
    assert "DEAL FOUND, BOOK NOW" in listing and "PRICE DROP" in listing

    assert cli.invoke(app, ["notify", "headline", "seats_open", "--reset"]).exit_code == 0
    assert headlines.load() == {}


@pytest.mark.parametrize(
    "args",
    [["nope", "x"], ["seats_open"], ["seats_open", "x", "--reset"], ["seats_open", "  "]],
)
def test_command_refuses_bad_input(args):
    result = cli.invoke(app, ["notify", "headline", *args])
    assert result.exit_code == 1
    assert headlines.load() == {}


def test_command_will_not_overwrite_a_broken_file():
    alerts_path().write_text("{not json", encoding="utf-8")
    assert cli.invoke(app, ["notify", "headline", "seats_open", "x"]).exit_code == 1
    assert alerts_path().read_text(encoding="utf-8") == "{not json"
