"""Finding out about, and installing, a newer jetbridge. Site fixes only help if they arrive.

The check is one plain request a day for a public file, carrying nothing about you. Set
JETBRIDGE_NO_UPDATE_CHECK=1 to stop it. An update only ever fast-forwards, and refuses to touch a
folder with local changes.
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import shutil
import subprocess
import urllib.request
from pathlib import Path

from jetbridge import autorun, claude_desktop, platforms
from jetbridge.store import Store

LATEST_URL = "https://raw.githubusercontent.com/xiuruichen/jetbridge/main/pyproject.toml"
CHECK_EVERY_S = 86400.0
TIMEOUT_S = 5
_LATEST = "latest_version"
_CHECKED_AT = "latest_version_checked_at"
_VERSION = re.compile(r'^version\s*=\s*"([0-9]+(?:\.[0-9]+)*)"', re.MULTILINE)


class UpdateProblem(Exception):
    """The update stopped. What's installed still works."""


def current() -> str:
    return importlib.metadata.version("jetbridge")


def _parts(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def version_in(pyproject: str) -> str | None:
    match = _VERSION.search(pyproject)
    return match[1] if match else None


def _fetch_latest() -> str | None:
    with urllib.request.urlopen(LATEST_URL, timeout=TIMEOUT_S) as reply:
        return version_in(reply.read().decode("utf-8", "replace"))


def refresh(store: Store, now: float) -> None:
    """Look for a new version at most once a day. Being offline just means looking later."""
    if os.environ.get("JETBRIDGE_NO_UPDATE_CHECK"):
        return
    last = float(store.get(_CHECKED_AT, "0") or 0)
    if now - last < CHECK_EVERY_S:
        return
    store.set(_CHECKED_AT, str(now))
    try:
        latest = _fetch_latest()
    except (OSError, ValueError):
        return
    if latest:
        store.set(_LATEST, latest)


def newer_version(store: Store, installed: str | None = None) -> str | None:
    latest = store.get(_LATEST)
    if not latest:
        return None
    try:
        return latest if _parts(latest) > _parts(installed or current()) else None
    except (ValueError, importlib.metadata.PackageNotFoundError):
        return None


def project_folder() -> Path | None:
    """The git checkout this is running from, if it is one."""
    folder = Path(__file__).resolve().parents[2]
    return folder if (folder / ".git").exists() else None


def _uv() -> str | None:
    found = shutil.which("uv")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / ("uv" if platforms.IS_MAC else "uv.exe")
    return str(local) if local.exists() else None


def _run(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(args),
        cwd=cwd,
        capture_output=True,
        text=True,
        errors="replace",
        check=False,
        **platforms.quiet(),
    )


def _refresh_launcher() -> None:
    launcher = platforms.launcher_path()
    if launcher.exists():
        launcher.write_bytes(platforms.launcher_bytes())


def install() -> list[str]:
    """Bring this copy up to date. Raises ``UpdateProblem`` and says why if it can't."""
    folder = project_folder()
    if folder is None:
        raise UpdateProblem("this copy of jetbridge wasn't installed with git, so can't update")
    git, uv = shutil.which("git"), _uv()
    if not git or not uv:
        raise UpdateProblem("updating needs git and uv, and one of them can't be found")

    changes = _run(git, "status", "--porcelain", "--untracked-files=no", cwd=folder)
    if changes.returncode != 0:
        raise UpdateProblem(f"git couldn't read the project folder: {changes.stderr.strip()}")
    if changes.stdout.strip():
        raise UpdateProblem(f"{folder} has local changes, so I left it alone")

    before = current()
    pulled = _run(git, "pull", "--ff-only", cwd=folder)
    if pulled.returncode != 0:
        raise UpdateProblem(f"couldn't download the update: {pulled.stderr.strip()}")

    synced = _run(uv, "sync", cwd=folder)
    if synced.returncode != 0:
        if "another process" in synced.stderr or "os error 32" in synced.stderr:
            raise UpdateProblem(
                "a jetbridge program is in use. Quit Claude Desktop fully, including from the "
                "system tray, and try again"
            )
        raise UpdateProblem(f"couldn't install the update: {synced.stderr.strip()}")

    report = [f"Updated from {before}."]
    _refresh_launcher()
    if autorun.is_installed():
        autorun.install()
    try:
        if claude_desktop.status() is claude_desktop.State.OUT_OF_DATE:
            claude_desktop.connect()
    except claude_desktop.ConfigProblem as exc:
        report.append(f"Couldn't refresh the Claude Desktop connection: {exc}")
    report.append("Restart Claude Desktop, if you use it, to pick up the new version.")
    return report
