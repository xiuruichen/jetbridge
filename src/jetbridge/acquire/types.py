"""Acquisition value types and errors."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Outcome(StrEnum):
    OK = "ok"
    BLOCKED = "blocked"  # bot-manager challenge, 401 or 403
    RATE_LIMITED = "rate_limited"
    ERROR = "error"  # network failure, 5xx, or the expected marker is absent


@dataclass
class RawResponse:
    status: int | None
    body: str
    tier: str = "T1"
    meta: dict[str, Any] = field(default_factory=dict)


class Blocked(Exception):
    """The site showed a challenge. Never retried: it opens a persisted cooldown."""


class SchemaDrift(Exception):
    """The response did not match the expected shape. Fail loudly, never as "no seats"."""
