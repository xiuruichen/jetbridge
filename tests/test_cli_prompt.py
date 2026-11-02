"""The `watch add` recipient prompt, run through the real command."""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from jetbridge.cli import app
from jetbridge.service import open_store

ARGS = ["watch", "add", "CX805", "--from", "YYZ", "--to", "HKG", "--dates", "2099-05-21"]
runner = CliRunner()


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))


def _watches():
    return open_store().list_watches()


def test_prompt_takes_other_peoples_addresses():
    result = runner.invoke(app, ARGS, input="Ann@example.com, bo@example.org\n")
    assert result.exit_code == 0, result.output
    assert "Send seat alerts to" in result.output
    assert _watches()[0].recipients == ("ann@example.com", "bo@example.org")


def test_pressing_enter_means_the_owner():
    result = runner.invoke(app, ARGS, input="\n")
    assert result.exit_code == 0, result.output
    assert _watches()[0].recipients == ()


def test_notify_option_skips_the_prompt():
    result = runner.invoke(app, [*ARGS, "--notify", "ann@example.com"])
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
