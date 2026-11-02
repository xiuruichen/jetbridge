"""Where mutable state lives.

Always outside the project folder: the project sits in OneDrive, and SQLite databases and
Chrome profiles are not safe (or small) in a synced folder.
"""

from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    override = os.environ.get("JETBRIDGE_HOME")
    if override:
        base = Path(override)
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


def raw_dir() -> Path:
    return _sub("raw")


def profile_dir(site: str) -> Path:
    return _sub("profiles") / site
