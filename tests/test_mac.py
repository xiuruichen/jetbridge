"""The Mac version's logic. These run anywhere, and say nothing about how a real Mac behaves."""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

import pytest

from jetbridge import autorun, claude_desktop, launchd, paths, platforms, power, updater, winsched

_open_private_window = platforms.open_private_window  # conftest stops tests opening real windows


@pytest.fixture
def mac(monkeypatch, tmp_path):
    monkeypatch.setattr(platforms, "IS_MAC", True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("JETBRIDGE_HOME", raising=False)
    return tmp_path


@pytest.fixture
def launchctl(monkeypatch):
    calls: list[tuple[str, ...]] = []
    loaded: set[str] = set()
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501, raising=False)

    class Result:
        def __init__(self, code=0):
            self.returncode, self.stdout, self.stderr = code, "", "refused"

    def run(*args):
        calls.append(args)
        if args[0] == "bootstrap":
            loaded.add(Path(args[2]).stem)
        elif args[0] == "bootout":
            loaded.discard(args[1].split("/")[-1])
        elif args[0] == "print":
            return Result(0 if args[1].split("/")[-1] in loaded else 1)
        return Result()

    monkeypatch.setattr(launchd, "_launchctl", run)
    return calls, loaded


# ---- where things go ------------------------------------------------------------------
def test_state_lives_in_application_support(mac):
    assert paths.home() == mac / "Library" / "Application Support" / "jetbridge"


def test_a_chosen_state_folder_still_wins(mac, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_HOME", str(mac / "elsewhere"))
    assert paths.home() == mac / "elsewhere"


def test_claude_desktop_config_is_found(mac):
    folder = mac / "Library" / "Application Support" / "Claude"
    folder.mkdir(parents=True)
    assert claude_desktop.config_files() == [folder / claude_desktop.CONFIG_NAME]


def test_no_claude_desktop_means_no_config(mac, monkeypatch):
    monkeypatch.setattr(claude_desktop, "_MAC_APP", mac / "missing.app")
    assert claude_desktop.config_files() == []


def test_the_right_python_for_each_system(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MAC", True)
    assert platforms.environment_python() == Path(sys.prefix) / "bin" / "python"
    monkeypatch.setattr(platforms, "IS_MAC", False)
    assert platforms.environment_python() == Path(sys.prefix) / "Scripts" / "python.exe"


def test_claude_desktop_starts_the_right_python(mac):
    entry = claude_desktop.server_entry()
    assert entry["command"].endswith("python") and entry["args"] == ["-m", "jetbridge.mcp_server"]


# ---- background checks ----------------------------------------------------------------
def test_the_right_scheduler_for_each_system(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MAC", True)
    assert autorun.task_name() == launchd.TASK_NAME
    monkeypatch.setattr(platforms, "IS_MAC", False)
    assert autorun.task_name() == winsched.TASK_NAME


def test_checks_run_by_the_clock_every_15_minutes():
    python = Path("/venv/bin/python")
    plist = launchd.tick_plist(python)
    assert plist["ProgramArguments"] == [str(python), "-m", "jetbridge", "tick"]
    assert [slot["Minute"] for slot in plist["StartCalendarInterval"]] == [0, 15, 30, 45]
    assert "StartInterval" not in plist and plist["RunAtLoad"] is False


def test_the_plist_is_valid_and_keeps_a_chosen_state_folder():
    data = plistlib.dumps(launchd.tick_plist(Path("/p"), "/custom"))
    assert plistlib.loads(data)["EnvironmentVariables"]["JETBRIDGE_HOME"] == "/custom"


def test_scheduled_output_goes_to_the_log():
    assert launchd.tick_plist(Path("/p"))["EnvironmentVariables"]["JETBRIDGE_LOG"] == "1"


def test_install_writes_the_agent_and_replaces_any_old_one(mac, launchctl):
    calls, _ = launchctl
    launchd.install()
    assert (mac / "Library" / "LaunchAgents" / f"{launchd.TASK_NAME}.plist").exists()
    assert [call[0] for call in calls] == ["bootout", "bootstrap"]
    assert calls[1][1] == "gui/501"
    assert launchd.is_installed()


def test_remove_leaves_nothing(mac, launchctl):
    launchd.install()
    launchd.remove()
    assert not launchd.is_installed()
    assert not (mac / "Library" / "LaunchAgents" / f"{launchd.TASK_NAME}.plist").exists()


def test_a_refused_install_says_why(mac, monkeypatch):
    monkeypatch.setattr(launchd.os, "getuid", lambda: 501, raising=False)

    class Refused:
        returncode, stdout, stderr = 1, "", "Bootstrap failed: 5"

    monkeypatch.setattr(launchd, "_launchctl", lambda *_: Refused())
    with pytest.raises(RuntimeError, match="Bootstrap failed"):
        launchd.install()


# ---- sleep ----------------------------------------------------------------------------
PMSET = """Battery Power:
 sleep                1
 displaysleep         2
AC Power:
 womp                 1
 sleep                10
 displaysleep         10
"""


def test_reads_the_plugged_in_sleep_timer():
    assert power.parse_pmset(PMSET) == 600


def test_reads_never_sleeping():
    assert (
        power.parse_pmset(PMSET.replace("sleep                10", "sleep                0")) == 0
    )


def test_an_unexpected_pmset_answer_is_refused():
    with pytest.raises(ValueError):
        power.parse_pmset("nothing useful")


def test_keep_awake_uses_caffeinate_only_on_mains_power():
    plist = launchd.awake_plist()
    assert plist["ProgramArguments"] == ["/usr/bin/caffeinate", "-s"] and plist["KeepAlive"] is True


def test_keep_awake_and_put_it_back(mac, launchctl, store, monkeypatch):
    monkeypatch.setattr(power, "_pmset", lambda: PMSET)
    assert not power.plugged_in_timers().stays_awake
    power.keep_awake(store)
    assert launchd.keeping_awake() and power.plugged_in_timers().stays_awake
    assert power.restore(store) is True
    assert not launchd.keeping_awake()
    assert power.restore(store) is False


def test_a_mac_set_to_never_sleep_changes_nothing(mac, launchctl, store, monkeypatch):
    monkeypatch.setattr(
        power, "_pmset", lambda: PMSET.replace("sleep                10", "sleep 0")
    )
    calls, _ = launchctl
    power.keep_awake(store)
    assert not [call for call in calls if call[0] == "bootstrap"]
    assert power.restore(store) is False


# ---- the command and windows ----------------------------------------------------------
def test_the_launcher_runs_through_python_on_each_system(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MAC", True)
    mac_text = platforms.launcher_bytes().decode()
    assert mac_text.startswith("#!/bin/sh\n") and '-m jetbridge "$@"' in mac_text
    monkeypatch.setattr(platforms, "IS_MAC", False)
    assert platforms.launcher_bytes().endswith(b'python.exe" -m jetbridge %*\r\n')


def test_updating_rewrites_the_launcher_as_a_runnable_script(mac):
    launcher = platforms.launcher_path()
    launcher.parent.mkdir(parents=True)
    launcher.write_text("old")
    updater._refresh_launcher()
    assert launcher.read_bytes().startswith(b"#!/bin/sh")


def test_a_terminal_command_with_quotes_stays_one_command():
    script = platforms.terminal_script('python -m jetbridge "a b"')
    assert 'do script "python -m jetbridge \\"a b\\""' in script


def test_a_refused_window_says_what_to_run(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MAC", True)

    class Refused:
        returncode = 1

    monkeypatch.setattr(platforms.subprocess, "run", lambda *_, **__: Refused())
    with pytest.raises(OSError, match="jetbridge notify setup-window"):
        _open_private_window("notify", "setup-window")


def test_the_notice_says_it_is_experimental():
    assert "experimental" in platforms.MAC_NOTICE and "real Mac" in platforms.MAC_NOTICE


def test_windows_opens_its_own_console(monkeypatch):
    monkeypatch.setattr(platforms, "IS_MAC", False)
    opened = []
    monkeypatch.setattr(platforms.subprocess, "Popen", lambda a, **k: opened.append((a, k)))
    _open_private_window("notify", "setup-window")
    args, options = opened[0]
    assert args[1:] == ["-m", "jetbridge", "notify", "setup-window"]
    assert options["creationflags"] == 0x10


# ---- the installer --------------------------------------------------------------------
INSTALLER = Path(__file__).parent.parent / "install.sh"


def test_the_installer_says_it_is_experimental_and_keeps_to_a_mac():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "EXPERIMENTAL" in text and 'uname -s)" = "Darwin"' in text


def test_a_cut_off_download_cant_run_half_the_installer():
    assert INSTALLER.read_text(encoding="utf-8").rstrip().endswith("\nmain")


def test_the_installer_only_fast_forwards_and_leaves_other_folders_alone():
    text = INSTALLER.read_text(encoding="utf-8")
    assert "pull --ff-only" in text and "isn't jetbridge, so I left it alone" in text


def test_the_installer_reads_answers_from_the_terminal():
    assert "read -r answer </dev/tty" in INSTALLER.read_text(encoding="utf-8")
