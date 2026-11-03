"""What `jetbridge doctor` and Claude's diagnose tool check."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from jetbridge import headlines, power, winsched
from jetbridge.alerts import format_when
from jetbridge.models import CheckStatus
from jetbridge.notify import email as mail
from jetbridge.notify import push
from jetbridge.store import Store


@dataclass(frozen=True)
class Check:
    label: str
    ok: bool
    fix: str = ""
    chat_fix: str = ""  # the same fix in Claude's terms, where it differs
    serious: bool = True  # False for what's expected on a new or empty setup


def _chrome_installed() -> bool:
    places = [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        places.append(str(Path(local) / "Google" / "Chrome" / "Application" / "chrome.exe"))
    return bool(shutil.which("chrome")) or any(Path(place).exists() for place in places)


def _last_clear_answer_at(store: Store) -> float | None:
    return store.conn.execute(
        "SELECT MAX(observed_at) AS t FROM observations WHERE status!=?",
        (CheckStatus.UNKNOWN.value,),
    ).fetchone()["t"]


def last_clear_answer(store: Store) -> str | None:
    at = _last_clear_answer_at(store)
    return format_when(at) if at is not None else None


def checks(store: Store) -> list[Check]:
    config = mail.load_config()
    password = (
        mail.keyring.get_password(mail.KEYRING_SERVICE, config.from_address) if config else None
    )
    failures = [
        row["last_error"]
        for row in store.conn.execute(
            "SELECT last_error FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL"
            " AND (recipient IS NULL OR recipient!=?)",
            (push.PUSH_RECIPIENT,),
        )
    ]
    headline_problem = headlines.problem()
    timers = power.plugged_in_timers()
    push_problem = store.get(push.LAST_ERROR) if push.enabled() else None
    return [
        Check(
            "Google Chrome installed",
            _chrome_installed(),
            "Install Google Chrome",
            "Ask them to install Google Chrome from google.com/chrome.",
        ),
        Check(
            "Email set up",
            bool(config and password),
            "Run: jetbridge notify setup",
            "Offer open_email_setup.",
        ),
        Check("Emails sending", not failures, failures[-1] if failures else ""),
        Check(
            "Phone alerts sending (optional)",
            push_problem is None,
            f"{push_problem}. Emails aren't affected",
            serious=False,
        ),
        Check("Alert headlines readable", headline_problem is None, headline_problem or ""),
        Check(
            "Scheduled task installed",
            winsched.is_installed(),
            "Run: jetbridge schedule install",
            "Offer to turn on automatic checks.",
        ),
        Check(
            "This PC stays awake while plugged in",
            timers is None or timers.stays_awake,
            "Checks stop while it sleeps. To keep it awake, run: jetbridge keep-awake on",
            "Checks stop while this PC sleeps. Offer to keep it awake while plugged in.",
            serious=False,
        ),
        Check(
            "At least one active watch",
            bool(store.list_watches("active")),
            "Run: jetbridge start",
            "Ask which flight they want to watch.",
            serious=False,
        ),
        Check(
            "A clear answer from an airline so far",
            _last_clear_answer_at(store) is not None,
            "Run: jetbridge check <id>",
            "Offer to check a watch now. A new watch's first check runs within about 15 minutes.",
            serious=False,
        ),
    ]
