"""Filesystem locations. Everything mutable lives under ``var/`` (git-ignored)."""

from __future__ import annotations

import os
from pathlib import Path


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def var_dir() -> Path:
    p = Path(os.environ.get("JETBRIDGE_VAR", project_root() / "var"))
    p.mkdir(parents=True, exist_ok=True)
    return p


def profile_dir(site: str) -> Path:
    """Persistent browser profile per site (cookies, bot-manager state, opt-in logins)."""
    p = var_dir() / "profiles" / site
    p.mkdir(parents=True, exist_ok=True)
    return p


def db_path() -> Path:
    return var_dir() / "jetbridge.sqlite3"


def blob_dir() -> Path:
    p = var_dir() / "blobs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir() -> Path:
    p = var_dir() / "reports"
    p.mkdir(parents=True, exist_ok=True)
    return p
