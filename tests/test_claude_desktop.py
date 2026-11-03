"""Connecting to Claude Desktop must never damage the person's own config file."""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from jetbridge import claude_desktop as desktop
from jetbridge.claude_desktop import State
from jetbridge.cli import app

OTHER = {"command": "other-server", "args": ["--flag"]}


@pytest.fixture
def machine(tmp_path, monkeypatch):
    """A PC with no Claude Desktop yet, and a server program that exists."""
    local, roaming = tmp_path / "Local", tmp_path / "Roaming"
    local.mkdir()
    roaming.mkdir()
    program = tmp_path / "venv" / "Scripts" / "python.exe"
    program.parent.mkdir(parents=True)
    program.write_text("")
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.delenv("JETBRIDGE_HOME", raising=False)
    monkeypatch.setattr(desktop, "server_program", lambda: program)
    return tmp_path


@pytest.fixture
def config(machine):
    """The standard install, not yet connected."""
    (machine / "Local" / "AnthropicClaude").mkdir()
    return machine / "Roaming" / "Claude" / desktop.CONFIG_NAME


def store_config(machine):
    (machine / "Local" / "Packages" / "Claude_abc123").mkdir(parents=True)
    return (
        machine
        / "Local"
        / "Packages"
        / "Claude_abc123"
        / "LocalCache"
        / "Roaming"
        / "Claude"
        / desktop.CONFIG_NAME
    )


def servers(path):
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


def test_nothing_is_written_when_the_app_is_not_installed(machine):
    assert desktop.status() is State.NOT_INSTALLED
    with pytest.raises(desktop.ConfigProblem, match="isn't installed"):
        desktop.connect()
    assert not (machine / "Roaming" / "Claude").exists()


def test_connect_creates_the_file_with_the_full_program_path(machine, config):
    assert desktop.connect() is True
    assert servers(config) == {
        "jetbridge": {
            "command": str(desktop.server_program()),
            "args": ["-m", "jetbridge.mcp_server"],
        }
    }
    assert desktop.status() is State.CONNECTED


def test_store_build_is_found_in_its_package_folder(machine):
    path = store_config(machine)
    assert desktop.config_files() == [path]
    desktop.connect()
    assert "jetbridge" in servers(path)


def test_both_files_stay_the_same_when_both_exist(machine, config):
    path = store_config(machine)
    desktop.connect()
    assert servers(path) == servers(config)
    assert desktop.status() is State.CONNECTED


def test_other_servers_and_settings_survive(machine, config):
    config.parent.mkdir(parents=True)
    original = {"mcpServers": {"other": OTHER}, "preferences": {"theme": "dark"}}
    config.write_text(json.dumps(original), encoding="utf-8")
    desktop.connect()
    saved = json.loads(config.read_text(encoding="utf-8"))
    assert saved["mcpServers"]["other"] == OTHER
    assert saved["preferences"] == {"theme": "dark"}
    backup = config.with_name(config.name + ".jetbridge-backup")
    assert json.loads(backup.read_text(encoding="utf-8")) == original


def test_a_byte_order_mark_is_understood(machine, config):
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": OTHER}}), encoding="utf-8-sig")
    desktop.connect()
    assert servers(config)["other"] == OTHER


@pytest.mark.parametrize("damaged", ['{"mcpServers": {', "[1, 2]", '{"mcpServers": []}'])
def test_a_damaged_file_is_left_exactly_as_it_was(machine, config, damaged):
    config.parent.mkdir(parents=True)
    config.write_text(damaged, encoding="utf-8")
    assert desktop.status() is State.UNREADABLE
    with pytest.raises(desktop.ConfigProblem, match="left it alone"):
        desktop.connect()
    with pytest.raises(desktop.ConfigProblem):
        desktop.disconnect()
    assert config.read_text(encoding="utf-8") == damaged
    assert [p.name for p in config.parent.iterdir()] == [config.name]


def test_one_damaged_file_stops_the_change_to_the_other(machine, config):
    store = store_config(machine)
    config.parent.mkdir(parents=True)
    config.write_text("not json", encoding="utf-8")
    with pytest.raises(desktop.ConfigProblem):
        desktop.connect()
    assert not store.exists()


def test_connecting_twice_changes_nothing_the_second_time(machine, config):
    desktop.connect()
    before = config.stat().st_mtime_ns
    assert desktop.connect() is False
    assert config.stat().st_mtime_ns == before


def test_an_old_path_is_noticed_and_replaced(machine, config):
    config.parent.mkdir(parents=True)
    old = {"mcpServers": {"jetbridge": {"command": "C:\\old\\jetbridge-mcp.exe"}}}
    config.write_text(json.dumps(old), encoding="utf-8")
    assert desktop.status() is State.OUT_OF_DATE
    desktop.connect()
    assert desktop.status() is State.CONNECTED


def test_a_missing_program_is_refused_rather_than_written(machine, config):
    desktop.server_program().unlink()
    with pytest.raises(desktop.ConfigProblem, match="missing"):
        desktop.connect()
    assert not config.exists()


def test_a_custom_state_folder_goes_with_the_entry(machine, config, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", "D:\\elsewhere")
    desktop.connect()
    assert servers(config)["jetbridge"]["env"] == {"JETBRIDGE_HOME": "D:\\elsewhere"}


def test_disconnect_removes_only_jetbridge(machine, config):
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"other": OTHER}}), encoding="utf-8")
    desktop.connect()
    assert desktop.disconnect() is True
    assert servers(config) == {"other": OTHER}
    assert desktop.status() is State.NOT_CONNECTED
    assert desktop.disconnect() is False


def test_cli_connect_says_to_restart_the_app(machine, config):
    result = CliRunner().invoke(app, ["claude", "connect"])
    assert result.exit_code == 0
    assert "Quit Claude Desktop" in result.output
    assert desktop.status() is State.CONNECTED


def test_cli_explains_when_the_app_is_missing(machine):
    result = CliRunner().invoke(app, ["claude", "connect"])
    assert result.exit_code == 1
    assert "isn't installed" in result.output


@pytest.mark.parametrize(("answer", "connected"), [("y\n", True), ("\n", True), ("n\n", False)])
def test_ask_only_connects_on_a_yes(machine, config, answer, connected):
    result = CliRunner().invoke(app, ["claude", "connect", "--ask"], input=answer)
    assert result.exit_code == 0
    assert (desktop.status() is State.CONNECTED) is connected


def test_ask_with_nobody_there_changes_nothing(machine, config):
    result = CliRunner().invoke(app, ["claude", "connect", "--ask"], input="")
    assert result.exit_code == 0
    assert not config.exists()


def test_ask_is_quiet_about_a_missing_app_and_does_not_fail(machine):
    result = CliRunner().invoke(app, ["claude", "connect", "--ask"])
    assert result.exit_code == 0
    assert "jetbridge claude connect" in result.output


def test_ask_does_not_ask_again_once_connected(machine, config):
    desktop.connect()
    result = CliRunner().invoke(app, ["claude", "connect", "--ask"])
    assert result.exit_code == 0
    assert "already connected" in result.output


def test_cli_status_and_disconnect(machine, config):
    desktop.connect()
    assert "Claude Desktop: connected" in CliRunner().invoke(app, ["claude", "status"]).output
    assert CliRunner().invoke(app, ["claude", "disconnect"]).exit_code == 0
    assert desktop.status() is State.NOT_CONNECTED
