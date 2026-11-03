"""The few things that differ between Windows and a Mac."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

IS_MAC = sys.platform == "darwin"
_CREATE_NO_WINDOW = 0x08000000
_CREATE_NEW_CONSOLE = 0x00000010
MAC_NOTICE = (
    "jetbridge on a Mac is experimental and hasn't been tried on a real Mac yet. Airline searches, "
    "sleep and Claude Desktop may not work as they do on Windows, and Chrome may appear during "
    "checks. Please tell us how it goes: https://github.com/xiuruichen/jetbridge/issues/new/choose"
)


def environment_python() -> Path:
    """This environment's own python, wherever it's being run from."""
    if IS_MAC:
        return Path(sys.prefix) / "bin" / "python"
    return Path(sys.prefix) / "Scripts" / "python.exe"


def quiet() -> dict[str, Any]:
    """Options that stop a helper program flashing a window on Windows."""
    return {} if IS_MAC else {"creationflags": _CREATE_NO_WINDOW}


def launcher_path() -> Path:
    """Where the `jetbridge` command lives. On a Mac, uv's installer puts its folder on the PATH."""
    if IS_MAC:
        return Path.home() / ".local" / "bin" / "jetbridge"
    from jetbridge.paths import home

    return home() / "bin" / "jetbridge.cmd"


def launcher_bytes() -> bytes:
    """Through python, so the running command doesn't lock a program an update has to replace."""
    python = environment_python()
    if IS_MAC:
        return f'#!/bin/sh\nexec "{python}" -m jetbridge "$@"\n'.encode()
    encoding = "oem" if sys.platform == "win32" else "utf-8"  # oem is how cmd.exe reads it
    return f'@"{python}" -m jetbridge %*\r\n'.encode(encoding, "replace")


def terminal_script(command: str) -> str:
    """The AppleScript that runs a command in a new Terminal window."""
    escaped = command.replace("\\", "\\\\").replace('"', '\\"')
    return f'tell application "Terminal"\nactivate\ndo script "{escaped}"\nend tell'


def open_private_window(*arguments: str) -> None:
    """Run a jetbridge command in its own window. Raises ``OSError`` if one can't be opened."""
    command = [sys.executable, "-m", "jetbridge", *arguments]
    if IS_MAC:
        script = terminal_script(shlex.join(command))
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=False, timeout=30
        )
        if result.returncode != 0:
            # macOS asks permission before one program can control Terminal, and it may be refused.
            raise OSError(f"run this in Terminal instead: jetbridge {' '.join(arguments)}")
        return
    subprocess.Popen(command, creationflags=_CREATE_NEW_CONSOLE)
