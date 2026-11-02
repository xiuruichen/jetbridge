"""Custom alert headlines, saved in alerts.json in the state folder.

A bad file must never cost anyone an alert, so reading falls back to the built-in wording and
`doctor` reports the problem. Only the headline changes: when alerts fire is not configurable.
"""

from __future__ import annotations

import json

from jetbridge.alerts import AlertKind
from jetbridge.paths import alerts_path

MAX_LENGTH = 80


def clean(text: str) -> str:
    """Headlines become email subjects, so they have to stay on one printable line."""
    headline = " ".join(text.split())
    if not headline:
        raise ValueError("a headline can't be empty")
    if len(headline) > MAX_LENGTH:
        raise ValueError(f"a headline can be at most {MAX_LENGTH} characters")
    if not headline.isprintable():
        raise ValueError("a headline can't contain control characters")
    return headline


def parse(data: object) -> dict[AlertKind, str]:
    if not isinstance(data, dict) or not isinstance(data.get("headlines", {}), dict):
        raise ValueError('expected {"headlines": {"seats_open": "..."}}')
    parsed: dict[AlertKind, str] = {}
    for name, text in data.get("headlines", {}).items():
        if not isinstance(text, str):
            raise ValueError(f"the headline for {name} has to be text")
        parsed[AlertKind(name)] = clean(text)  # an unknown name raises ValueError too
    return parsed


def _read() -> dict[AlertKind, str]:
    path = alerts_path()
    if not path.exists():
        return {}
    return parse(json.loads(path.read_text(encoding="utf-8")))


def problem() -> str | None:
    try:
        _read()
    except (OSError, ValueError) as exc:
        return f"{alerts_path()}: {exc}"
    return None


def load() -> dict[AlertKind, str]:
    try:
        return _read()
    except (OSError, ValueError):
        return {}


def save(headlines: dict[AlertKind, str]) -> None:
    data = {"headlines": {kind.value: text for kind, text in headlines.items()}}
    alerts_path().write_text(json.dumps(data, indent=2), encoding="utf-8")
