"""The `watch add` recipient prompt, run through the real command."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jetbridge.cli import app
from jetbridge.notify import email as mail
from jetbridge.service import open_store

ARGS = ["watch", "add", "CX805", "--from", "YYZ", "--to", "HKG", "--dates", "2099-05-21"]
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))


def _watches():
    return open_store().list_watches()


def test_prompt_takes_other_peoples_addresses():
    result = runner.invoke(app, ARGS, input="Ann@example.com, bo@example.org\ny\n")
    assert result.exit_code == 0, result.output
    assert "Send seat alerts to" in result.output
    assert _watches()[0].recipients == ("ann@example.com", "bo@example.org")


def test_pressing_enter_means_the_owner():
    result = runner.invoke(app, ARGS, input="\ny\n")
    assert result.exit_code == 0, result.output
    assert _watches()[0].recipients == ()


def test_notify_option_skips_the_prompt():
    result = runner.invoke(app, [*ARGS, "--notify", "ann@example.com"], input="y\n")
    assert result.exit_code == 0 and "Send seat alerts to" not in result.output
    assert _watches()[0].recipients == ("ann@example.com",)


def test_typo_at_the_prompt_adds_nothing():
    result = runner.invoke(app, ARGS, input="not-an-email\n")
    assert result.exit_code == 1 and not _watches()


def test_no_one_to_answer_adds_nothing_and_explains():
    result = runner.invoke(app, ARGS, input="")
    assert result.exit_code == 1 and not _watches()
    assert "--notify" in result.output


def test_bad_date_fails_before_asking():
    result = runner.invoke(app, [*ARGS[:-1], "soon"])
    assert result.exit_code == 1 and "Send seat alerts to" not in result.output


def test_watch_add_shows_the_review_and_waits_for_a_yes():
    result = runner.invoke(app, [*ARGS, "--notify", "me"], input="n\n")
    assert result.exit_code == 0
    assert "Flight:      CX805 from YYZ to HKG" in result.output
    assert "nothing was saved" in result.output
    assert not _watches()


def test_pressing_enter_at_the_review_saves_nothing():
    runner.invoke(app, [*ARGS, "--notify", "me"], input="\n")
    assert not _watches()


def test_no_one_to_answer_the_review_saves_nothing_and_explains():
    result = runner.invoke(app, [*ARGS, "--notify", "me"], input="")
    assert result.exit_code == 1 and not _watches()
    assert "--yes" in result.output


def test_scripts_can_skip_the_review_by_asking_to():
    result = runner.invoke(app, [*ARGS, "--notify", "me", "--yes"])
    assert result.exit_code == 0 and len(_watches()) == 1
    assert "Start watching" not in result.output


def test_version_is_shown():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0 and result.output.startswith("jetbridge ")


def test_help_starts_with_start_and_hides_the_scheduled_command():
    output = runner.invoke(app, ["--help"]).output
    assert output.index("start") < output.index("watch")
    assert "tick" not in output


def test_email_setup_rejects_addresses_that_cannot_work(monkeypatch):
    saved = []
    monkeypatch.setattr(mail, "save_config", lambda config, password: saved.append(config))
    monkeypatch.setattr(mail.GmailSender, "send", lambda *_, **__: None)
    answers = "nonsense\nme@gmail.com\nbad address\nme@gmail.com\n   \nabcd efgh\n"
    result = runner.invoke(app, ["notify", "setup"], input=answers)
    assert result.exit_code == 0, result.output
    assert result.output.count("look like an email") == 2
    assert "That was empty" in result.output
    assert saved == [mail.EmailConfig("me@gmail.com", "me@gmail.com")]
