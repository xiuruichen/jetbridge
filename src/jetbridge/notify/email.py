"""Email through Gmail SMTP, plus the outbox flush.

Addresses are in config.json in the state folder. The Gmail app password is stored only in
Windows Credential Manager through ``keyring``, and is never written to a file or a log.
"""

from __future__ import annotations

import json
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Protocol

import keyring

from jetbridge.paths import config_path
from jetbridge.store import Store

KEYRING_SERVICE = "jetbridge-gmail"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_S = 30
DIGEST_MAX_AGE_S = 86400


class NotConfigured(Exception):
    pass


class Sender(Protocol):
    def send(self, subject: str, body: str) -> None: ...


@dataclass(frozen=True)
class EmailConfig:
    from_address: str
    to_address: str


def load_config() -> EmailConfig | None:
    path = config_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if not data.get("from_address") or not data.get("to_address"):
        return None
    return EmailConfig(data["from_address"], data["to_address"])


def save_config(config: EmailConfig, app_password: str) -> None:
    keyring.set_password(KEYRING_SERVICE, config.from_address, app_password.replace(" ", ""))
    config_path().write_text(
        json.dumps({"from_address": config.from_address, "to_address": config.to_address}),
        encoding="utf-8",
    )


class GmailSender:
    def send(self, subject: str, body: str) -> None:
        config = load_config()
        if config is None:
            raise NotConfigured("email isn't set up. Run: jetbridge notify setup")
        password = keyring.get_password(KEYRING_SERVICE, config.from_address)
        if not password:
            raise NotConfigured("no saved app password. Run: jetbridge notify setup")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = config.from_address
        message["To"] = config.to_address
        message.set_content(body)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.login(config.from_address, password)
            smtp.send_message(message)


def flush_outbox(store: Store, sender: Sender, now: float) -> int:
    """Send what's pending, oldest first. A failure stops the flush, and everything stays
    queued for the next tick, so nothing is lost and order is preserved."""
    sent = 0
    for item in store.pending_alerts():
        if item.kind == "digest" and now - item.created_at > DIGEST_MAX_AGE_S:
            store.drop_alert(item.id)  # a stale summary is noise; real alerts are always kept
            continue
        try:
            sender.send(item.subject, item.body)
        except Exception as exc:
            store.mark_failed(item.id, f"{type(exc).__name__}: {exc}"[:300])
            break
        store.mark_sent(item.id, now)
        sent += 1
    return sent
