"""User-facing request and profile types (what Claude fills in via MCP)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from jetbridge.models.core import Cabin, DateWindow


class SpecModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DestinationPref(SpecModel):
    """A destination (airport or metro/region code resolved by the KB) with a preference weight.

    weight 1.0 = ideal; lower = acceptable but less preferred. The optimizer turns
    (1 - weight) into a penalty, so a cheaper option to a less-preferred airport
    must be *meaningfully* cheaper to win.
    """

    code: str = Field(description="IATA airport (HKG), metro (TYO), or KB region (ASIA_EAST)")
    weight: float = Field(default=1.0, ge=0.0, le=1.0)


class Preferences(SpecModel):
    """The traveller's trade-offs, in US dollars per passenger. This is the utility
    model the optimizer maximizes; Claude elicits it ("how much is a lie-flat seat
    on a 14h flight worth to you?") instead of guessing.

    Cabin value is *per flown hour* above economy, so business on a 14h transpacific
    segment matters ~10x more than on a 90-minute domestic hop.
    """

    cabin_premium_usd_per_hour: dict[Cabin, float] = Field(
        default_factory=lambda: {
            Cabin.ECONOMY: 0,
            Cabin.PREMIUM: 45,
            Cabin.BUSINESS: 130,
            Cabin.FIRST: 170,
        },
        description="willingness to pay per flown hour, relative to economy",
    )
    value_of_time_usd_per_hour: float = Field(default=30, ge=0)
    stop_penalty_usd: float = Field(default=60, ge=0)
    overnight_layover_penalty_usd: float = Field(default=300, ge=0, description="incl. hotel")
    long_layover_hours: float = Field(
        default=6, gt=0, description="layovers above this are flagged"
    )
    airport_change_penalty_usd: float = Field(default=150, ge=0)
    separate_ticket_penalty_usd: float = Field(
        default=120, ge=0, description="hassle + misconnect exposure of a positioning ticket"
    )
    risk_aversion: float = Field(
        default=0.5, ge=0, le=1, description="0 = ignore fragility, 1 = strongly avoid it"
    )
    risk_unit_usd: float = Field(
        default=1500,
        ge=0,
        description="cost per pax of a fully-realized risk (e.g. space vanishing)",
    )

    def cabin_value(self, cabin: Cabin) -> float:
        return float(self.cabin_premium_usd_per_hour.get(cabin, 0))


class SearchSpec(SpecModel):
    origins: list[str] = Field(min_length=1, description="IATA airports or metro codes")
    destinations: list[DestinationPref] = Field(min_length=1)
    outbound: DateWindow
    inbound: DateWindow | None = Field(default=None, description="None = one-way")
    passengers: int = Field(ge=1, le=9)
    preferred_cabin: Cabin = Cabin.BUSINESS
    min_cabin: Cabin = Field(
        default=Cabin.ECONOMY, description="never return options whose main cabin is below this"
    )
    min_cabin_mix: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="minimum fraction of flown time in the main cabin or better",
    )
    together: Literal["same_flights", "same_day", "any"] = "same_flights"
    max_stops: int = Field(default=3, ge=0, le=4)
    max_duration_hours: float = Field(default=40, gt=0)
    include_positioning: bool = Field(
        default=True, description="consider separate-ticket flights to US gateways"
    )
    nearby_origins: list[str] = Field(
        default_factory=list, description="alternate departure airports user can drive to"
    )
    programs: list[str] | None = Field(
        default=None, description="restrict to these program ids; None = all reachable"
    )
    include_cash: bool = True
    preferences: Preferences = Field(default_factory=Preferences)
    refine: bool = Field(
        default=True,
        description="after the first pass, run targeted follow-up searches "
        "(gateway positioning, split-seat completion)",
    )
    refine_budget: int = Field(default=240, ge=0, le=2000, description="max follow-up queries")
    time_budget_minutes: float = Field(
        default=20,
        gt=0,
        le=600,
        description="target wall-clock for the whole search. Queries are ranked by "
        "priority; those beyond the budget are DEFERRED (recorded, resumable with "
        "`resume --deep`), never silently dropped.",
    )

    @field_validator("origins", "nearby_origins")
    @classmethod
    def _upper(cls, v: list[str]) -> list[str]:
        return [c.strip().upper() for c in v]

    @model_validator(mode="after")
    def _check(self) -> SearchSpec:
        if self.min_cabin > self.preferred_cabin:
            raise ValueError("min_cabin cannot exceed preferred_cabin")
        if self.inbound and self.inbound.start < self.outbound.start:
            raise ValueError("inbound window starts before outbound window")
        return self


class Profile(SpecModel):
    """Balances and valuations. Balances are in each currency's own units."""

    balances: dict[str, int] = Field(
        default_factory=dict,
        description="currency id -> balance. Program ids (american, delta, united, ...) "
        "or card currencies (amex_mr, chase_ur, citi_ty, capital_one, bilt)",
    )
    cpp_overrides: dict[str, Decimal] = Field(
        default_factory=dict, description="currency id -> cents per point valuation"
    )
    logins_enabled: list[str] = Field(
        default_factory=list, description="adapter ids allowed to use a logged-in session"
    )
    home_currency: str = "USD"
