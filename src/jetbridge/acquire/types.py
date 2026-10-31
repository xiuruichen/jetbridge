"""Acquisition-layer value types."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from typing import Any, Literal

from jetbridge.models.core import AwardOffer, Cabin, CashOffer


class Outcome(StrEnum):
    OK = "ok"
    EMPTY = "empty"  # valid response, no availability (a real answer, not a failure)
    BLOCKED = "blocked"  # bot-manager challenge / 403
    RATE_LIMITED = "rate_limited"
    ERROR = "error"  # network / 5xx / unparseable


@dataclass(frozen=True)
class Query:
    """A single search request against one adapter."""

    adapter: str
    kind: Literal["day", "calendar"]
    origin: str
    destination: str
    date: date
    pax: int = 1
    cabin: Cabin | None = None  # hint for adapters that search per cabin

    @property
    def key(self) -> str:
        c = self.cabin.value if self.cabin else "-"
        return f"{self.adapter}:{self.kind}:{self.origin}-{self.destination}:{self.date}:p{self.pax}:{c}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter,
            "kind": self.kind,
            "origin": self.origin,
            "destination": self.destination,
            "date": self.date.isoformat(),
            "pax": self.pax,
            "cabin": self.cabin.value if self.cabin else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Query:
        return cls(
            adapter=d["adapter"],
            kind=d["kind"],
            origin=d["origin"],
            destination=d["destination"],
            date=date.fromisoformat(d["date"]),
            pax=d["pax"],
            cabin=Cabin(d["cabin"]) if d.get("cabin") else None,
        )


@dataclass
class RawResponse:
    """Exactly what came off the wire, plus how we got it."""

    status: int | None
    body: str
    tier: Literal["T1", "T2", "T3", "replay"]
    started_at: datetime
    latency_ms: int
    meta: dict[str, Any] = field(default_factory=dict)

    def sha(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8", "replace")).hexdigest()


@dataclass(frozen=True)
class CalendarPoint:
    """Cheapest price per day from a calendar view. Used only for *sound* pruning."""

    day: date
    cabin: Cabin | None  # None = the calendar is not cabin-specific
    miles: int | None  # None = no availability that day
    complete: bool  # whether this calendar is known to include partner space


@dataclass
class ParseResult:
    awards: list[AwardOffer] = field(default_factory=list)
    cash: list[CashOffer] = field(default_factory=list)
    calendar: list[CalendarPoint] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.awards or self.cash or self.calendar)


class AdapterError(Exception):
    """Base for adapter failures that should be recorded in the ledger."""

    outcome: Outcome = Outcome.ERROR


class Blocked(AdapterError):
    outcome = Outcome.BLOCKED


class RateLimited(AdapterError):
    outcome = Outcome.RATE_LIMITED


class SchemaDrift(AdapterError):
    """Response parsed as JSON but did not match the expected shape. Fail loud."""

    outcome = Outcome.ERROR


def stable_hash(obj: Any) -> str:
    return hashlib.sha1(json.dumps(obj, sort_keys=True, default=str).encode()).hexdigest()[:16]
