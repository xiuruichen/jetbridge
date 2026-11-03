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

from jetbridge.notify import push
from jetbridge.paths import config_path
from jetbridge.store import NewAlert, Store

KEYRING_SERVICE = "jetbridge-gmail"
SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465
SMTP_TIMEOUT_S = 30
OTHER_PERSON_FOOTER = """

--
You're getting this because someone added your address to a jetbridge flight watch.
To stop these emails, reply and ask them to remove you.
"""
DIGEST_MAX_AGE_S = 86400


class NotConfigured(Exception):
    pass


class BadRecipient(Exception):
    """The mail server refused this one address. Other emails can still go out."""


class Sender(Protocol):
    def send(self, subject: str, body: str, to: str | None = None) -> None: ...


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


def build_message(config: EmailConfig, subject: str, body: str, to: str | None) -> EmailMessage:
    """``to`` of None means the owner's address. Each email has exactly one recipient."""
    address = to or config.to_address
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.from_address
    message["To"] = address
    message.set_content(body if address == config.to_address else body + OTHER_PERSON_FOOTER)
    return message


class GmailSender:
    def send(self, subject: str, body: str, to: str | None = None) -> None:
        config = load_config()
        if config is None:
            raise NotConfigured("email isn't set up. Run: jetbridge notify setup")
        password = keyring.get_password(KEYRING_SERVICE, config.from_address)
        if not password:
            raise NotConfigured("no saved app password. Run: jetbridge notify setup")
        message = build_message(config, subject, body, to)
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=SMTP_TIMEOUT_S) as smtp:
            smtp.login(config.from_address, password)
            try:
                smtp.send_message(message)
            except smtplib.SMTPRecipientsRefused as exc:
                raise BadRecipient(str(exc)) from exc


def _flush_pushes(store: Store, now: float) -> None:
    """Phone alerts go first and on their own: neither kind of failure holds up the other."""
    for item in store.pending_alerts():
        if item.recipient != push.PUSH_RECIPIENT:
            continue
        if now - item.created_at > push.MAX_AGE_S:
            store.drop_alert(item.id)
            continue
        try:
            push.deliver(item.subject, item.body)
        except Exception as exc:
            store.mark_failed(item.id, f"{type(exc).__name__}: {exc}"[:300])
            store.set(push.LAST_ERROR, f"{type(exc).__name__}: {exc}"[:300])
            continue
        store.mark_sent(item.id, now)
        store.delete(push.LAST_ERROR)


def flush_outbox(store: Store, sender: Sender, now: float) -> int:
    """Send what's pending, oldest first. A failed email stops the emails, and they stay queued
    for the next tick, so nothing is lost and order is preserved. Returns the emails sent."""
    _flush_pushes(store, now)
    sent = 0
    for item in store.pending_alerts():
        if item.recipient == push.PUSH_RECIPIENT:
            continue
        if item.kind == "digest" and now - item.created_at > DIGEST_MAX_AGE_S:
            store.drop_alert(item.id)  # a stale summary is noise; real alerts are always kept
            continue
        try:
            sender.send(item.subject, item.body, item.recipient)
        except BadRecipient as exc:
            if item.recipient is None:  # the owner's own address: keep it, doctor reports it
                store.mark_failed(item.id, f"BadRecipient: {exc}"[:300])
                break
            # One bad address must not hold up everyone else, so tell the owner instead.
            store.drop_alert(item.id)
            store.enqueue(
                NewAlert(
                    "undeliverable",
                    f"jetbridge couldn't email {item.recipient}: {item.subject}",
                    f"The mail server refused {item.recipient}. Fix the address with "
                    f"'jetbridge watch recipients'. This is the alert they missed:\n\n{item.body}",
                ),
                now,
            )
            continue
        except Exception as exc:
            store.mark_failed(item.id, f"{type(exc).__name__}: {exc}"[:300])
            break
        store.mark_sent(item.id, now)
        sent += 1
    return sent
