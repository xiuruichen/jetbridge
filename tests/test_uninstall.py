"""Uninstalling removes jetbridge's things and nothing else."""

from __future__ import annotations

import json
import uuid
import winreg

import pytest
from typer.testing import CliRunner

from jetbridge import claude_desktop, uninstall, winsched
from jetbridge.cli import app
from jetbridge.notify import email as mail


@pytest.fixture
def machine(tmp_path, monkeypatch):
    state = tmp_path / "state"
    (state / "bin").mkdir(parents=True)
    (state / "jetbridge.db").write_text("")
    (state / "config.json").write_text(json.dumps({"from_address": "o@x.com", "to_address": "o@x"}))
    monkeypatch.setenv("JETBRIDGE_HOME", str(state))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    (tmp_path / "Local" / "AnthropicClaude").mkdir(parents=True)
    config = tmp_path / "Roaming" / "Claude" / claude_desktop.CONFIG_NAME
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps({"mcpServers": {"jetbridge": {"command": "x"}, "other": {"command": "y"}}})
    )
    steps = []
    monkeypatch.setattr(winsched, "is_installed", lambda: True)
    monkeypatch.setattr(winsched, "remove", lambda: steps.append("task"))
    monkeypatch.setattr(
        uninstall.keyring, "delete_password", lambda *args: steps.append(("password", args))
    )
    monkeypatch.setattr(uninstall, "remove_from_user_path", lambda folder: steps.append("path"))
    return tmp_path, state, config, steps


def test_saying_no_removes_nothing(machine):
    _root, state, config, steps = machine
    result = CliRunner().invoke(app, ["uninstall"], input="n\n")
    assert "nothing was removed" in result.output
    assert state.exists()
    assert steps == []
    assert "jetbridge" in json.loads(config.read_text())["mcpServers"]


def test_nobody_there_to_answer_removes_nothing(machine):
    _root, state, _config, steps = machine
    CliRunner().invoke(app, ["uninstall"], input="")
    assert state.exists()
    assert steps == []


def test_a_yes_removes_jetbridge_and_keeps_other_servers(machine):
    _root, state, config, steps = machine
    result = CliRunner().invoke(app, ["uninstall"], input="y\n")
    assert result.exit_code == 0
    assert not state.exists()
    assert steps == ["task", ("password", (mail.KEYRING_SERVICE, "o@x.com")), "path"]
    assert json.loads(config.read_text())["mcpServers"] == {"other": {"command": "y"}}
    assert "revoke the app password" in result.output
    assert "delete the jetbridge project folder" in result.output


def test_one_failure_does_not_stop_the_rest(machine, monkeypatch):
    _root, state, _config, _steps = machine

    def fail():
        raise RuntimeError("access denied")

    monkeypatch.setattr(winsched, "remove", fail)
    result = CliRunner().invoke(app, ["uninstall", "--yes"])
    assert "access denied" in result.output
    assert not state.exists()


@pytest.fixture
def scratch_key():
    name = f"Software\\jetbridge-test-{uuid.uuid4().hex}"
    winreg.CreateKey(winreg.HKEY_CURRENT_USER, name)
    yield name
    winreg.DeleteKey(winreg.HKEY_CURRENT_USER, name)


def read_path(name):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name) as key:
        return winreg.QueryValueEx(key, "Path")


def write_path(name, value):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, name, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, "Path", 0, winreg.REG_EXPAND_SZ, value)


def test_path_loses_only_that_folder_and_keeps_variables(scratch_key):
    write_path(scratch_key, r"%USERPROFILE%\x;C:\JB\Bin\;C:\y")
    assert uninstall.remove_from_user_path(r"C:\jb\bin", scratch_key) is True
    assert read_path(scratch_key) == (r"%USERPROFILE%\x;C:\y", winreg.REG_EXPAND_SZ)
    assert uninstall.remove_from_user_path(r"C:\jb\bin", scratch_key) is False


def test_no_path_at_all_is_fine(scratch_key):
    assert uninstall.remove_from_user_path(r"C:\jb\bin", scratch_key) is False
