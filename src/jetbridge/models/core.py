"""Canonical domain model.

Every adapter normalizes into these types. They are deliberately strict
(``extra="forbid"``, frozen) so that schema drift in a source surfaces as an
error instead of silently producing wrong data.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Cabin(StrEnum):
    ECONOMY = "Y"
    PREMIUM = "W"
    BUSINESS = "J"
    FIRST = "F"

    @property
    def rank(self) -> int:
        return {"Y": 0, "W": 1, "J": 2, "F": 3}[self.value]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Cabin):
            return NotImplemented
        return self.rank >= other.rank

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Cabin):
            return NotImplemented
        return self.rank > other.rank

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Cabin):
            return NotImplemented
        return self.rank <= other.rank

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        if not isinstance(other, Cabin):
            return NotImplemented
        return self.rank < other.rank


class Money(Strict):
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @classmethod
    def usd(cls, amount: float | int | str | Decimal) -> Money:
        return cls(amount=Decimal(str(amount)), currency="USD")

    def __add__(self, other: Money) -> Money:
        if other.currency != self.currency:
            raise ValueError(f"currency mismatch {self.currency} vs {other.currency}")
        return Money(amount=self.amount + other.amount, currency=self.currency)

    def scale(self, n: int) -> Money:
        return Money(amount=self.amount * n, currency=self.currency)

    def __str__(self) -> str:
        return f"{self.currency} {self.amount:,.2f}"


IATA = str  # 3-letter airport code
_IATA_PATTERN = r"^[A-Z]{3}$"
_CARRIER_PATTERN = r"^[A-Z0-9]{2}$"


class Segment(Strict):
    """One flight leg. Times are timezone-aware local times."""

    marketing_carrier: str = Field(pattern=_CARRIER_PATTERN)
    flight_number: str = Field(pattern=r"^\d{1,4}[A-Z]?$")
    operating_carrier: str | None = Field(default=None, pattern=_CARRIER_PATTERN)
    origin: IATA = Field(pattern=_IATA_PATTERN)
    destination: IATA = Field(pattern=_IATA_PATTERN)
    departs_at: datetime
    arrives_at: datetime
    cabin: Cabin
    booking_class: str | None = Field(default=None, pattern=r"^[A-Z]$")
    equipment: str | None = None

    @field_validator("departs_at", "arrives_at")
    @classmethod
    def _tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None or v.utcoffset() is None:
            raise ValueError("segment times must be timezone-aware")
        return v

    @model_validator(mode="after")
    def _ordering(self) -> Segment:
        if self.arrives_at <= self.departs_at:
            raise ValueError(f"segment arrives before it departs: {self.flight_code}")
        if self.duration > timedelta(hours=22):
            raise ValueError(f"implausible segment duration {self.duration}: {self.flight_code}")
        return self

    @property
    def operator(self) -> str:
        return self.operating_carrier or self.marketing_carrier

    @property
    def flight_code(self) -> str:
        return f"{self.marketing_carrier}{self.flight_number}"

    @property
    def duration(self) -> timedelta:
        return self.arrives_at - self.departs_at


class Itinerary(Strict):
    """An ordered chain of segments travelled on one ticket."""

    segments: tuple[Segment, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _chain(self) -> Itinerary:
        for a, b in zip(self.segments, self.segments[1:], strict=False):
            if b.departs_at <= a.arrives_at:
                raise ValueError(f"segment {b.flight_code} departs before {a.flight_code} arrives")
        return self

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def departs_at(self) -> datetime:
        return self.segments[0].departs_at

    @property
    def arrives_at(self) -> datetime:
        return self.segments[-1].arrives_at

    @property
    def duration(self) -> timedelta:
        return self.arrives_at - self.departs_at

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def airport_changes(self) -> list[tuple[str, str]]:
        return [
            (a.destination, b.origin)
            for a, b in zip(self.segments, self.segments[1:], strict=False)
            if a.destination != b.origin
        ]

    @property
    def flight_key(self) -> str:
        """Stable identity of the physical flights (independent of price/source)."""
        return "|".join(
            f"{s.flight_code}@{s.origin}{s.departs_at.date().isoformat()}" for s in self.segments
        )

    def cabin_mix(self, cabin: Cabin) -> float:
        """Fraction of flown time spent in ``cabin`` or better."""
        total = sum((s.duration for s in self.segments), timedelta())
        good = sum((s.duration for s in self.segments if s.cabin >= cabin), timedelta())
        return good / total if total else 0.0

    @property
    def main_cabin(self) -> Cabin:
        """Cabin of the longest segment, which is what people actually care about."""
        return max(self.segments, key=lambda s: s.duration).cabin


class Provenance(Strict):
    adapter: str
    tier: Literal["T1", "T2", "T3", "replay"]
    fetch_id: str
    observed_at: datetime
    request_pax: int = Field(ge=1, le=9)


class SeatInfo(Strict):
    """What we actually know about seat availability.

    ``exact``: the source states the number of seats.
    ``lower_bound``: we know at least ``count`` seats exist (e.g. a search with
    pax=count succeeded) but the true number may be higher.
    """

    kind: Literal["exact", "lower_bound"]
    count: int = Field(ge=1)

    def covers(self, pax: int) -> bool:
        return self.count >= pax

    def merge(self, other: SeatInfo) -> SeatInfo:
        if self.kind == "exact" and other.kind == "exact":
            return self if self.count >= other.count else other  # newest should be passed second
        if self.kind == "exact":
            return self
        if other.kind == "exact":
            return other
        return self if self.count >= other.count else other


class AwardOffer(Strict):
    program: str  # e.g. "american", "aeroplan" (KB program id)
    itinerary: Itinerary
    miles: int = Field(gt=0, description="per passenger")
    taxes: Money = Field(description="per passenger")
    seats: SeatInfo
    provenance: Provenance
    booking_url: str | None = None
    notes: tuple[str, ...] = ()

    @property
    def offer_id(self) -> str:
        h = hashlib.sha1(
            f"{self.program}|{self.itinerary.flight_key}|{self.itinerary.main_cabin}|{self.miles}".encode()
        ).hexdigest()
        return f"aw_{h[:12]}"


class CashOffer(Strict):
    source: str  # "google_flights", "american", ...
    itinerary: Itinerary
    price: Money = Field(description="per passenger, all-in")
    seats: SeatInfo | None = None
    provenance: Provenance
    booking_url: str | None = None

    @property
    def offer_id(self) -> str:
        h = hashlib.sha1(
            f"{self.source}|{self.itinerary.flight_key}|{self.itinerary.main_cabin}|{self.price.amount}".encode()
        ).hexdigest()
        return f"ca_{h[:12]}"


Offer = AwardOffer | CashOffer


class DateWindow(Strict):
    start: date
    end: date

    @model_validator(mode="after")
    def _order(self) -> DateWindow:
        if self.end < self.start:
            raise ValueError("date window end before start")
        if (self.end - self.start).days > 62:
            raise ValueError("date window longer than 62 days; split the search")
        return self

    def days(self) -> list[date]:
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]
