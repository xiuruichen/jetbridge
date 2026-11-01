"""Knowledge base: curated, versioned static data with strict schemas.

Loaded once and cached. Every file carries ``last_verified`` so reports can
state how fresh the partnership / transfer / chart data is.
"""

from __future__ import annotations

import math
from datetime import date
from decimal import Decimal
from functools import cache
from importlib import resources
from typing import Literal
from zoneinfo import ZoneInfo

import airportsdata
import yaml
from pydantic import BaseModel, ConfigDict, Field

from jetbridge.models.core import Cabin

_CARRIER = r"^[A-Z0-9]{2}$"


class _M(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BookableCarriers(_M):
    high: list[str] = Field(default_factory=list)
    verify: list[str] = Field(default_factory=list)

    def model_post_init(self, _ctx: object) -> None:
        import re

        for c in self.high + self.verify:
            if not re.match(_CARRIER, c):
                raise ValueError(f"bad carrier code in KB: {c!r}")


class Program(_M):
    id: str
    name: str
    currency: str
    adapter: str
    pricing: dict[str, str]
    award_chart: str | None
    bookable_carriers: BookableCarriers
    booking_url: str
    notes: list[str] = Field(default_factory=list)
    # North American airports with long-haul Asia departures bookable with this
    # program (own metal + partners). Used by the refinement stage to search
    # gateway->Asia space that the through-search may hide.
    gateways: list[str] = Field(default_factory=list)

    def can_book(self, carrier: str) -> Literal["high", "verify", None]:
        if carrier in self.bookable_carriers.high:
            return "high"
        if carrier in self.bookable_carriers.verify:
            return "verify"
        return None


class Transfer(_M):
    source: str
    program: str
    ratio: float = Field(gt=0)
    time: str
    confidence: Literal["high", "verify"]


class Airport(_M):
    iata: str
    name: str
    city: str
    country: str
    lat: float
    lon: float
    tz: str

    @property
    def zone(self) -> ZoneInfo:
        return ZoneInfo(self.tz)


class KB(_M):
    programs: dict[str, Program]
    transfers: list[Transfer]
    currencies: dict[str, str]
    metros: dict[str, list[str]]
    regions: dict[str, list[str]]
    gateways: list[str]
    nearby: dict[str, list[str]]
    mct: dict[str, int]
    charts: dict
    cpp: dict[str, Decimal]
    last_verified: dict[str, date]
    regional_operators: dict[str, list[str]] = Field(default_factory=dict)
    na_nonstop_asia: list[str] = Field(default_factory=list)
    metro_expanding_adapters: dict[str, list[str]] = Field(default_factory=dict)

    def bookable(
        self, program: str, operator: str, marketing: str
    ) -> Literal["high", "verify", None]:
        """Can ``program`` ticket this segment? Regional operators flying for a
        bookable mainline brand count as that brand."""
        p = self.programs[program]
        conf = p.can_book(operator)
        if conf:
            return conf
        if marketing in self.regional_operators.get(operator, []):
            return p.can_book(marketing)
        return None

    # ---- geography -------------------------------------------------------
    def airport(self, iata: str) -> Airport:
        return _airport(iata)

    def expand(self, code: str) -> list[str]:
        """Resolve region / metro / airport codes to a sorted list of airports."""
        code = code.upper()
        if code in self.regions:
            out: set[str] = set()
            for c in self.regions[code]:
                # Region members are sub-regions or literal airports, never metros.
                out.update(self.expand(c) if c in self.regions else [_airport(c).iata])
            return sorted(out)
        if code in self.metros:
            return list(self.metros[code])
        _airport(code)  # validates
        return [code]

    def region_of(self, iata: str) -> str | None:
        for name in ("ASIA_EAST", "ASIA_SOUTHEAST", "ASIA_SOUTH"):
            if iata in self.expand(name):
                return name
        return None

    def distance_miles(self, a: str, b: str) -> float:
        pa, pb = _airport(a), _airport(b)
        return _haversine_miles(pa.lat, pa.lon, pb.lat, pb.lon)

    def is_domestic_us(self, a: str, b: str) -> bool:
        return _airport(a).country == "US" and _airport(b).country == "US"

    # ---- programs / currencies -----------------------------------------
    def programs_for_carrier(self, carrier: str) -> list[tuple[str, str]]:
        out = []
        for pid, p in self.programs.items():
            conf = p.can_book(carrier)
            if conf:
                out.append((pid, conf))
        return out

    def funding_sources(self, program: str) -> list[Transfer]:
        """All ways to obtain ``program`` miles: the program itself (ratio 1) + transfers."""
        own = Transfer(source=program, program=program, ratio=1.0, time="n/a", confidence="high")
        return [own, *[t for t in self.transfers if t.program == program]]

    def cpp_for(self, currency: str, overrides: dict[str, Decimal] | None = None) -> Decimal:
        if overrides and currency in overrides:
            return Decimal(overrides[currency])
        if currency not in self.cpp:
            raise KeyError(f"no cpp valuation for currency {currency!r}")
        return self.cpp[currency]

    def mct_minutes(self, kind: str) -> int:
        return self.mct[kind]


def _haversine_miles(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@cache
def _airports() -> dict[str, dict]:
    return airportsdata.load("IATA")


@cache
def _airport(iata: str) -> Airport:
    rec = _airports().get(iata.upper())
    if rec is None:
        raise KeyError(f"unknown airport {iata!r}")
    return Airport(
        iata=rec["iata"],
        name=rec["name"],
        city=rec["city"],
        country=rec["country"],
        lat=rec["lat"],
        lon=rec["lon"],
        tz=rec["tz"],
    )


def _read(name: str) -> dict:
    text = resources.files("jetbridge.kb").joinpath("data", name).read_text(encoding="utf-8")
    return yaml.safe_load(text)


@cache
def load_kb() -> KB:
    programs_raw = _read("programs.yaml")
    transfers_raw = _read("transfers.yaml")
    geo = _read("geo.yaml")
    charts = _read("charts.yaml")
    vals = _read("valuations.yaml")

    programs = {pid: Program(id=pid, **p) for pid, p in programs_raw["programs"].items()}
    transfers = [
        Transfer(source=src, program=prog, **t)
        for src, targets in transfers_raw["transfers"].items()
        for prog, t in targets.items()
    ]
    currencies = {cid: c["name"] for cid, c in transfers_raw["currencies"].items()}
    for p in programs.values():
        currencies.setdefault(p.currency, p.name)

    kb = KB(
        programs=programs,
        transfers=transfers,
        currencies=currencies,
        metros=geo["metros"],
        regions=geo["regions"],
        gateways=geo["gateways"],
        nearby=geo["nearby"],
        mct=geo["mct"],
        charts=charts,
        cpp={k: Decimal(str(v)) for k, v in vals["cpp"].items()},
        regional_operators=programs_raw.get("regional_operators", {}),
        na_nonstop_asia=geo.get("na_nonstop_asia", []),
        metro_expanding_adapters=geo.get("metro_expanding_adapters", {}),
        last_verified={
            "programs": programs_raw["last_verified"],
            "transfers": transfers_raw["last_verified"],
            "geo": geo["last_verified"],
            "charts": charts["last_verified"],
            "valuations": vals["last_verified"],
        },
    )
    _validate(kb)
    return kb


def _validate(kb: KB) -> None:
    """Cross-file integrity checks: fail at startup, not mid-search."""
    for t in kb.transfers:
        if t.program not in kb.programs:
            raise ValueError(f"transfer to unknown program {t.program}")
        if t.source not in kb.currencies:
            raise ValueError(f"transfer from unknown currency {t.source}")
    for cur in kb.currencies:
        if cur not in kb.cpp:
            raise ValueError(f"currency {cur} has no cpp valuation")
    for region in kb.regions:
        kb.expand(region)  # raises on unknown airport
    for g in kb.gateways:
        _airport(g)
    for p in kb.programs.values():
        for g in p.gateways:
            _airport(g)
    for cabin in Cabin:
        for band in kb.charts["plausibility"].values():
            if cabin.value not in band:
                raise ValueError(f"plausibility band missing cabin {cabin}")
