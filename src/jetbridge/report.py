"""A problem report someone can read and then choose to post. Nothing is sent from here.

When an airline changes its site, whoever fixes jetbridge needs the new shape of the reply, not
what's in it. So a saved reply is reduced to field names and types, with every value removed, and
names that could carry data are hidden too. Watches, addresses and error text are left out.
"""

from __future__ import annotations

import json
import platform
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jetbridge import doctor, updater
from jetbridge.paths import raw_dir
from jetbridge.store import Store

ISSUE_URL = "https://github.com/xiuruichen/jetbridge/issues/new"
MAX_URL_BODY = 6000
_FIELD_NAME = re.compile(r"^[A-Za-z_]{1,40}$")
_EMAIL = re.compile(r"[^\s@\"']+@[^\s@\"']+")
_NG_STATE = re.compile(r'<script id="ng-state"[^>]*>(.*?)</script>', re.DOTALL)
_MAX_DEPTH = 7
_MAX_FIELDS = 40


def structure(value: Any, depth: int = 0) -> Any:
    """The shape of a reply: field names and types, and never a value."""
    if depth >= _MAX_DEPTH:
        return "..."
    if isinstance(value, dict):
        shown: dict[str, Any] = {}
        for key in list(value)[:_MAX_FIELDS]:
            name = key if isinstance(key, str) and _FIELD_NAME.match(key) else "<name hidden>"
            shown.setdefault(name, structure(value[key], depth + 1))
        return shown
    if isinstance(value, list):
        return [structure(value[0], depth + 1), f"{len(value)} items"] if value else []
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, int | float):
        return "number"
    return "null" if value is None else "text"


def _as_data(body: str) -> Any:
    """The JSON in a saved reply, wherever a site keeps it. None if there isn't any."""
    found = _NG_STATE.search(body)
    candidates = [found[1]] if found else []
    candidates.append(body)
    candidates.extend(line[5:] for line in body.splitlines() if line.startswith("data:"))
    for text in candidates:
        try:
            return json.loads(text)
        except ValueError:
            continue
    return None


def _latest_replies(folder: Path) -> list[Path]:
    """The newest saved reply for each site and kind of problem."""
    latest: dict[str, Path] = {}
    for path in sorted(folder.glob("*.html")):
        parts = path.stem.split("-")
        if len(parts) >= 4:
            latest["-".join(parts[2:4])] = path
    return list(latest.values())


def _reply_section(path: Path) -> list[str]:
    parts = path.stem.split("-")
    heading = f"**{parts[2]}, {parts[3]}**"
    data = _as_data(path.read_text(encoding="utf-8", errors="replace"))
    if data is None:
        return [heading, "Not JSON, so its structure isn't included."]
    return [heading, "```json", json.dumps(structure(data), indent=1), "```"]


@dataclass(frozen=True)
class Report:
    title: str
    body: str
    url: str


def build(store: Store) -> Report:
    """Everything in this is meant to be safe to post, and it's still shown to the person first."""
    checks = [f"- {'OK' if check.ok else 'FAIL'}: {check.label}" for check in doctor.checks(store)]
    lines = [
        "**What happened?** (please add a sentence or two)",
        "",
        f"jetbridge {updater.current()} on {platform.system()} {platform.release()}",
        "",
        "**Checks**",
        *checks,
    ]
    replies = _latest_replies(raw_dir())
    if replies:
        lines += ["", "**Structure of the latest saved replies, with every value removed**"]
        for path in replies:
            lines += ["", *_reply_section(path)]
    body = _EMAIL.sub("<address>", "\n".join(lines))
    title = "Problem report"
    short = body if len(body) <= MAX_URL_BODY else body[:MAX_URL_BODY] + "\n\n(shortened to fit)"
    query = urllib.parse.urlencode({"title": title, "body": short})
    return Report(title, body, f"{ISSUE_URL}?{query}")
