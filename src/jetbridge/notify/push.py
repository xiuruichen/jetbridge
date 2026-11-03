"""Optional phone alerts through ntfy, for when minutes matter.

Off until someone turns it on. The alert text passes through ntfy.sh, and anyone who knows the topic
name can read it, so the name is long and random, stays in the state folder and is never shown in a
chat. Only seat alerts go this way, and each one also goes by email as usual.
"""

from __future__ import annotations

import json
import secrets
import urllib.request

from jetbridge.alerts import AlertKind
from jetbridge.paths import home
from jetbridge.store import NewAlert

SERVER = "https://ntfy.sh"
PUSH_RECIPIENT = "push:phone"
MAX_AGE_S = 3600.0  # a phone buzz about seats from hours ago would only mislead
TIMEOUT_S = 15
LAST_ERROR = "push_last_error"
_HIGH = 4
_KINDS = (AlertKind.SEATS_OPEN, AlertKind.SEATS_OPEN_UNCONFIRMED, AlertKind.PRICE_DROP)


def _path():
    return home() / "push.json"


def topic() -> str | None:
    try:
        return json.loads(_path().read_text(encoding="utf-8"))["topic"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def enabled() -> bool:
    return topic() is not None


def turn_on() -> str:
    """Make a topic, or keep the existing one so a subscribed phone keeps working."""
    existing = topic()
    if existing:
        return existing
    name = f"jetbridge-{secrets.token_hex(16)}"
    _path().write_text(json.dumps({"topic": name}), encoding="utf-8")
    return name


def turn_off() -> None:
    _path().unlink(missing_ok=True)


def wanted_for(kind: AlertKind) -> bool:
    return kind in _KINDS and enabled()


def alert_for(kind: AlertKind, subject: str, booking_url: str) -> NewAlert:
    body = json.dumps(
        {"message": "Tap to check and book. Seats can vanish quickly.", "click": booking_url}
    )
    return NewAlert(kind.value, subject, body, recipient=PUSH_RECIPIENT)


def _post(payload: dict) -> None:
    request = urllib.request.Request(
        SERVER,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT_S):
        pass


def deliver(subject: str, body: str) -> None:
    """Send one queued phone alert. Raises if it couldn't go."""
    name = topic()
    if name is None:
        raise LookupError("phone alerts are off")
    fields = json.loads(body)
    _post(
        {
            "topic": name,
            "title": subject,
            "message": fields["message"],
            "click": fields["click"],
            "priority": _HIGH,
        }
    )


def send_test() -> None:
    name = topic()
    if name is None:
        raise LookupError("phone alerts are off")
    _post(
        {
            "topic": name,
            "title": "jetbridge test",
            "message": "If you can read this, seat alerts will reach this phone.",
        }
    )
