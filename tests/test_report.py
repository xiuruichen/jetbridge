"""Problem reports must be useful to whoever fixes jetbridge, and carry nothing personal."""

from __future__ import annotations

import json
import urllib.parse

import pytest
from conftest import add_cx
from mcp import Client
from typer.testing import CliRunner

from jetbridge import doctor, mcp_server, report, service
from jetbridge.cli import app
from jetbridge.notify import email as mail
from jetbridge.paths import raw_dir

SECRETS = ["ann@example.com", "CX805", "110000", "Secret Traveler", "ABC123", "2027-05-21"]
REPLY = {
    "passenger": {"name": "Secret Traveler", "email": "ann@example.com", "record": "ABC123"},
    "slices": [{"flight": "CX805", "miles": 110000, "day": "2027-05-21", "open": True}],
    "CX805": {"value": None},
    "2027-05-21": 1,
    "empty": [],
}


def _save(body: str, name: str = "20270101-120000-delta-drift-T1.html") -> None:
    (raw_dir() / name).write_text(body, encoding="utf-8")


# ---- reducing a reply to its shape -----------------------------------------------------
def test_structure_keeps_field_names_and_types():
    shape = report.structure(REPLY)
    assert shape["passenger"]["email"] == "text"
    assert shape["slices"][0]["miles"] == "number"
    assert shape["slices"][0]["open"] == "true/false"
    assert shape["slices"][1] == "1 items"
    assert shape["empty"] == []


def test_structure_removes_every_value():
    text = json.dumps(report.structure(REPLY))
    for secret in SECRETS:
        assert secret not in text


def test_names_that_could_carry_data_are_hidden():
    shape = report.structure({"CX805": 1, "2027-05-21": 2, "a@b.co": 3, "fine": 4})
    assert set(shape) == {"<name hidden>", "fine"}


def test_very_deep_or_wide_replies_stay_small():
    deep: dict = {}
    for _ in range(50):
        deep = {"inner": deep}
    assert "..." in json.dumps(report.structure(deep))
    wide = {f"field_{chr(97 + i % 26)}{chr(97 + i // 26)}": i for i in range(500)}
    assert len(report.structure(wide)) <= 40


# ---- reading what each site saves ------------------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        json.dumps(REPLY),
        f'<html><script id="ng-state" type="application/json">{json.dumps(REPLY)}</script></html>',
        f"event: x\ndata:{json.dumps(REPLY)}\n",
    ],
)
def test_each_sites_format_is_read(body):
    assert report._as_data(body) == REPLY


def test_a_plain_page_is_left_out_entirely():
    _save("<html><title>Secret Traveler</title>ann@example.com</html>")
    built = report.build(service.open_store())
    assert "Secret Traveler" not in built.body and "isn't included" in built.body


# ---- the whole report -------------------------------------------------------------------
def test_nothing_personal_reaches_the_report(store, clock, monkeypatch):
    add_cx(store, clock, recipients=("ann@example.com",))
    store.enqueue(mail.NewAlert("x", "s", "b", recipient="ann@example.com"), clock())
    store.mark_failed(1, "SMTP refused ann@example.com at C:\\Users\\Secret Traveler")
    monkeypatch.setattr(
        doctor.mail, "load_config", lambda: mail.EmailConfig("ann@example.com", "ann@example.com")
    )
    _save(json.dumps(REPLY))
    built = report.build(store)
    for text in (built.body, urllib.parse.unquote_plus(built.url)):
        for secret in SECRETS:
            assert secret not in text
        assert "SMTP refused" not in text


def test_it_says_what_failed_and_what_version(store):
    built = report.build(store)
    assert "FAIL: Email set up" in built.body and "jetbridge " in built.body


def test_it_only_keeps_the_newest_reply_of_each_kind(store):
    _save(json.dumps({"older": 1}), "20270101-120000-delta-drift-T1.html")
    _save(json.dumps({"newer": 1}), "20270102-120000-delta-drift-T1.html")
    _save(json.dumps({"other": 1}), "20270101-120000-united-drift-T1.html")
    body = report.build(store).body
    assert "newer" in body and "other" in body and "older" not in body


def test_a_long_report_still_fits_in_a_link(store):
    wide = {"".join(chr(97 + (i >> s) % 26) for s in (0, 5, 10)): "x" for i in range(4000)}
    for number in range(10):
        _save(json.dumps(wide), f"20270101-12000{number}-site{number}-drift-T1.html")
    built = report.build(store)
    assert "shortened to fit" in urllib.parse.unquote_plus(built.url)
    assert len(built.url) < 30_000


def test_the_link_goes_to_the_issue_form(store):
    assert report.build(store).url.startswith("https://github.com/xiuruichen/jetbridge/issues/new?")


# ---- both ways in -----------------------------------------------------------------------
def test_the_command_shows_it_and_posts_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    output = CliRunner().invoke(app, ["report"]).output
    assert "Nothing has been sent" in output and "issues/new" in output


async def test_chat_shows_it_and_says_to_read_it_first(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))
    async with Client(mcp_server.server) as client:
        answer = await client.call_tool("prepare_problem_report", {})
    data = answer.structured_content
    assert data["sent"] is False and "issues/new" in data["open_to_post"]
    assert "read" in data["note"].lower()
