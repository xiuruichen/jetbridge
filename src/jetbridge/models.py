"""Domain types shared by every layer."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum


class Cabin(StrEnum):
    ECONOMY = "economy"
    PREMIUM = "premium"
    BUSINESS = "business"
    FIRST = "first"


class CheckStatus(StrEnum):
    AVAILABLE = "available"  # the flight has seats in the requested cabin
    NOT_OFFERED = "not_offered"  # valid answer: flight listed, no qualifying seats
    NOT_LISTED = "not_listed"  # valid answer: flight absent from the results
    UNKNOWN = "unknown"  # we could not tell. Never treated as "no seats".

    @property
    def conclusive(self) -> bool:
        return self is not CheckStatus.UNKNOWN


_FLIGHT = re.compile(r"^\s*([A-Za-z0-9]{2})\s*0*(\d{1,4})\s*$")


def normalize_number(raw: str | int) -> str:
    return str(raw).strip().lstrip("0") or "0"


@dataclass(frozen=True)
class Flight:
    carrier: str
    number: str

    @classmethod
    def parse(cls, text: str) -> Flight:
        match = _FLIGHT.match(text)
        if not match:
            raise ValueError(f"not a flight number: {text!r} (expected something like AA100)")
        return cls(match.group(1).upper(), normalize_number(match.group(2)))

    @property
    def code(self) -> str:
        return f"{self.carrier}{self.number}"


@dataclass(frozen=True)
class Watch:
    id: int
    flight: Flight
    origin: str
    destination: str
    dates: tuple[date, ...]
    cabin: Cabin
    program: str = "american"
    pax: int = 1
    checks_per_day: int = 6
    max_miles: int | None = None
    recipients: tuple[str, ...] = ()  # seat alerts go here; empty means the owner's address
    status: str = "active"  # active | paused | finished
    created_at: float = 0.0
    next_check_at: float = 0.0

    @property
    def label(self) -> str:
        return f"{self.flight.code} {self.origin}-{self.destination} {self.cabin.value}"


@dataclass(frozen=True)
class Query:
    """One search request. Searches always use the watch's real party size."""

    origin: str
    destination: str
    date: date
    pax: int = 1
    program: str = "american"


@dataclass(frozen=True)
class Leg:
    carrier: str
    number: str
    origin: str
    destination: str
    departs_at: datetime
    arrives_at: datetime
    operating_carrier: str | None = None
    equipment: str | None = None

    @property
    def code(self) -> str:
        return f"{self.carrier}{self.number}"


@dataclass(frozen=True)
class ProductOffer:
    """One bookable product on an itinerary, e.g. BUSINESS for 110,000 miles."""

    cabin: Cabin
    leg_cabins: tuple[Cabin, ...]  # the cabin actually flown on each leg
    miles: int
    taxes: Decimal
    currency: str
    seats_remaining: int  # 0 means the site did not say (NOT sold out)
    notes: tuple[str, ...] = ()
    seats_at_least: int = 0  # a capped count: the site says "9 or more"


@dataclass(frozen=True)
class ParsedSlice:
    legs: tuple[Leg, ...]
    offers: tuple[ProductOffer, ...]
    unknown_fares: tuple[str, ...] = ()  # fares we couldn't place in a cabin


@dataclass(frozen=True)
class UnreadableSlice:
    flight_hints: frozenset[str]  # flight codes we could still recover; may be empty
    reason: str


@dataclass
class ParsedResponse:
    """A response the adapter positively recognized as a genuine search answer."""

    slices: list[ParsedSlice] = field(default_factory=list)
    unreadable: list[UnreadableSlice] = field(default_factory=list)
    site_error: str | None = None  # an error code in the site's answer


class AnswerKind(StrEnum):
    ANSWER = "answer"
    BLOCKED = "blocked"  # challenge page, 403 or 429
    DRIFT = "drift"  # the page no longer looks the way the parser expects
    ERROR = "error"  # network failure, 5xx, browser failure
    DEFERRED = "deferred"  # no request was made: cooldown, rate budget or time limit


@dataclass(frozen=True)
class SearchAnswer:
    kind: AnswerKind
    parsed: ParsedResponse | None = None
    detail: str | None = None
    retry_at: float | None = None  # when a DEFERRED or BLOCKED query may run again


@dataclass(frozen=True)
class CheckResult:
    """The outcome of one flight, on one date, in one search."""

    status: CheckStatus
    miles: int | None = None
    taxes: Decimal | None = None
    currency: str | None = None
    seats_exact: int | None = None  # exact count when the site states one
    seats_at_least: int | None = None  # otherwise, the party size that was searched
    notes: tuple[str, ...] = ()
    detail: str | None = None
    suspect_parser: bool = False  # UNKNOWN because the response looked wrong

    @property
    def seats_text(self) -> str:
        if self.seats_exact is not None:
            return str(self.seats_exact)
        if self.seats_at_least is not None:
            return f"at least {self.seats_at_least}"
        return "unknown"
