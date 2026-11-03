"""Where mutable state lives.

Always outside the project folder: it may be in a synced folder such as OneDrive, and SQLite
databases and Chrome profiles are not safe (or small) there.
"""

from __future__ import annotations

import os
from pathlib import Path

from jetbridge import platforms


def home() -> Path:
    override = os.environ.get("JETBRIDGE_HOME")
    if override:
        base = Path(override)
    elif platforms.IS_MAC:
        base = Path.home() / "Library" / "Application Support" / "jetbridge"
    else:
        local = os.environ.get("LOCALAPPDATA")
        base = Path(local) / "jetbridge" if local else Path.home() / ".jetbridge"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _sub(name: str) -> Path:
    path = home() / name
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return home() / "jetbridge.db"


def lock_path() -> Path:
    return home() / "tick.lock"


def log_path() -> Path:
    return home() / "jetbridge.log"


def config_path() -> Path:
    return home() / "config.json"


def alerts_path() -> Path:
    return home() / "alerts.json"


def raw_dir() -> Path:
    return _sub("raw")


def profile_dir(site: str) -> Path:
    return _sub("profiles") / site
