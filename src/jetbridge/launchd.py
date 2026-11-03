"""Running things in the background on a Mac, through LaunchAgents. Experimental.

An agent runs only while the person is logged in, which Chrome needs anyway. Timing is by the clock
rather than by interval, because launchd documents intervals inconsistently around sleep. Nothing
here can wake a sleeping Mac, so a check runs once it's awake again.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
from pathlib import Path
from typing import Any

from jetbridge.platforms import environment_python

TASK_NAME = "com.jetbridge.tick"
AWAKE_NAME = "com.jetbridge.keep-awake"
CAFFEINATE = "/usr/bin/caffeinate"


def _agents() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path(label: str) -> Path:
    return _agents() / f"{label}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args], capture_output=True, text=True, errors="replace", check=False
    )


def tick_plist(python: Path, state_folder: str | None = None) -> dict[str, Any]:
    environment = {"JETBRIDGE_LOG": "1"}
    if state_folder:
        environment["JETBRIDGE_HOME"] = state_folder
    return {
        "Label": TASK_NAME,
        "ProgramArguments": [str(python), "-m", "jetbridge", "tick"],
        "StartCalendarInterval": [{"Minute": minute} for minute in (0, 15, 30, 45)],
        "RunAtLoad": False,
        "ProcessType": "Background",
        "EnvironmentVariables": environment,
        "WorkingDirectory": str(Path.home()),
    }


def awake_plist() -> dict[str, Any]:
    """caffeinate -s stops sleep only on mains power, and ends with the agent. It can't keep a Mac
    awake with its lid closed."""
    return {
        "Label": AWAKE_NAME,
        "ProgramArguments": [CAFFEINATE, "-s"],
        "RunAtLoad": True,
        "KeepAlive": True,
    }


def _load(label: str, plist: dict[str, Any]) -> None:
    path = _plist_path(label)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(plist))
    _launchctl("bootout", f"{_domain()}/{label}")  # replacing an older copy, if there is one
    result = _launchctl("bootstrap", _domain(), str(path))
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "launchctl failed")


def _unload(label: str) -> None:
    _launchctl("bootout", f"{_domain()}/{label}")
    _plist_path(label).unlink(missing_ok=True)


def _is_loaded(label: str) -> bool:
    return _launchctl("print", f"{_domain()}/{label}").returncode == 0


def install() -> None:
    _load(TASK_NAME, tick_plist(environment_python(), os.environ.get("JETBRIDGE_HOME")))


def remove() -> None:
    _unload(TASK_NAME)


def is_installed() -> bool:
    return _is_loaded(TASK_NAME)


def start_keep_awake() -> None:
    _load(AWAKE_NAME, awake_plist())


def stop_keep_awake() -> None:
    _unload(AWAKE_NAME)


def keeping_awake() -> bool:
    return _is_loaded(AWAKE_NAME)
