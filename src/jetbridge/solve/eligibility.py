"""Candidates: hard filters, quality analysis, structural risk, sanity flags.

A Candidate is one way to fly ONE passenger-direction: a priced primary offer
(award or cash), optionally preceded by separately-ticketed positioning legs
(``extras``, cash). Hard constraints are never relaxed; soft issues become
flags, quality penalties and a structural risk score the optimizer prices in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from jetbridge.kb import KB
from jetbridge.models.core import AwardOffer, Cabin, CashOffer, Itinerary
from jetbridge.models.spec import SearchSpec
from jetbridge.solve.quality import Quality, analyze, penalty_usd


@dataclass
class Candidate:
    offer: AwardOffer | CashOffer
    direction: str  # out|in
    dest_weight: float
    flags: list[str] = field(default_factory=list)
    extras: tuple[CashOffer, ...] = ()  # positioning legs flown BEFORE the primary offer
    extras_after: tuple[CashOffer, ...] = ()  # positioning legs flown AFTER it (return trips)
    quality: Quality | None = None
    risk: float = 0.0
    risk_reasons: list[str] = field(default_factory=list)
    penalty_usd: float = 0.0  # per pax: quality + destination + risk (pre-verification)

    @property
    def id(self) -> str:
        """Solver identity: direction + searched party size + any positioning legs."""
        base = f"{self.offer.offer_id}:{self.direction}:p{self.offer.provenance.request_pax}"
        return (
            base
            + "".join(f"<{x.offer_id}" for x in self.extras)
            + "".join(f">{x.offer_id}" for x in self.extras_after)
        )

    @property
    def itinerary(self) -> Itinerary:
        if not (self.extras or self.extras_after):
            return self.offer.itinerary
        return Itinerary(segments=tuple(s for part in self.offers for s in part.itinerary.segments))

    @property
    def is_award(self) -> bool:
        return isinstance(self.offer, AwardOffer)

    @property
    def composed(self) -> bool:
        return bool(self.extras or self.extras_after)

    @property
    def positioning(self) -> tuple[CashOffer, ...]:
        return (*self.extras, *self.extras_after)

    @property
    def group(self) -> str:
        return self.itinerary.flight_key

    @property
    def cash_per_pax(self) -> Decimal:
        o = self.offer
        base = o.taxes.amount if isinstance(o, AwardOffer) else o.price.amount
        return base + sum((x.price.amount for x in self.positioning), Decimal(0))

    @property
    def offers(self) -> list[AwardOffer | CashOffer]:
        return [*self.extras, self.offer, *self.extras_after]


@dataclass
class FilterStats:
    seen: int = 0
    kept: int = 0
    rejected: dict[str, int] = field(default_factory=dict)

    def reject(self, why: str) -> None:
        self.rejected[why] = self.rejected.get(why, 0) + 1


def build(
    offer: AwardOffer | CashOffer,
    direction: str,
    spec: SearchSpec,
    origins: list[str],
    dests: dict[str, float],
    kb: KB,
    *,
    extras: tuple[CashOffer, ...] = (),
    extras_after: tuple[CashOffer, ...] = (),
    now: datetime | None = None,
) -> tuple[Candidate | None, str | None]:
    """Apply hard constraints and compute quality/risk. Returns (candidate, reject_reason)."""
    try:
        cand = Candidate(offer, direction, 0.0, extras=extras, extras_after=extras_after)
        it = cand.itinerary  # validates chain ordering of composed legs
    except ValueError as exc:
        return None, f"invalid composition: {str(exc)[:80]}"
    o, d = (it.origin, it.destination) if direction == "out" else (it.destination, it.origin)
    if o not in origins:
        return None, "origin not requested"
    if d not in dests:
        return None, "destination not requested"
    window = spec.outbound if direction == "out" else spec.inbound
    assert window is not None
    if not (window.start <= it.departs_at.date() <= window.end):
        return None, "departure outside date window"
    main = offer.itinerary.main_cabin  # cabin of the priced long-haul product
    if main < spec.min_cabin:
        return None, f"main cabin below minimum {spec.min_cabin.name}"
    if it.cabin_mix(main) < spec.min_cabin_mix:
        return None, "too much time below the main cabin (cabin mix)"
    if it.stops > spec.max_stops + (1 if cand.composed else 0):
        return None, "too many stops"
    if it.duration > timedelta(hours=spec.max_duration_hours):
        return None, "too long"

    # ticket boundaries: index i means segment i+1 starts a different ticket
    sep: set[int] = set()
    k = -1
    parts = cand.offers
    for part in parts[:-1]:
        k += len(part.itinerary.segments)
        sep.add(k)
    q = analyze(it, kb, spec.preferences, spec.preferred_cabin, separate_ticket_after=sep)
    if any(lv.separate_ticket and lv.tight for lv in q.layovers):
        return None, "positioning connection below separate-ticket buffer"
    cand.dest_weight = dests[d]
    cand.quality = q
    cand.flags = list(q.flags)

    risk_parts: list[tuple[float, str]] = []
    for part in cand.offers:
        if isinstance(part, AwardOffer):
            cand.flags += _sanity(part, kb)
            prog = kb.programs.get(part.program)
            if prog:
                for s in part.itinerary.segments:
                    conf = kb.bookable(part.program, s.operator, s.marketing_carrier)
                    if conf is None:
                        cand.flags.append(f"{s.operator} not in KB partner list for {part.program}")
                        risk_parts.append((0.1, f"{s.operator} partnership not in KB"))
                    elif conf == "verify":
                        cand.flags.append(
                            f"{part.program}<->{s.operator} partnership marked 'verify' in KB"
                        )
                        risk_parts.append((0.05, "partnership flagged 'verify'"))
            if part.seats.kind == "exact" and part.seats.count <= spec.passengers:
                risk_parts.append((0.35, f"last {part.seats.count} seat(s) at this price"))
            elif part.seats.kind == "exact" and part.seats.count == spec.passengers + 1:
                risk_parts.append((0.15, f"only {part.seats.count} seats left"))
        age = (now or datetime.now(UTC)) - part.provenance.observed_at
        if age > timedelta(hours=6):
            risk_parts.append((0.1, f"observed {age.total_seconds() / 3600:.0f}h ago"))
    if any("price anomaly" in f for f in cand.flags):
        risk_parts.append((0.25, "price outside plausible band"))
    if cand.composed:
        risk_parts.append(
            (0.25, "separate tickets: a delay on the positioning flight is not protected")
        )
    if any(lv.tight and not lv.separate_ticket for lv in q.layovers):
        risk_parts.append((0.2, "tight connection"))
    if any(lv.airport_change for lv in q.layovers):
        risk_parts.append((0.1, "airport change"))
    survive = 1.0
    for r, _ in risk_parts:
        survive *= 1 - r
    cand.risk = 1 - survive
    cand.risk_reasons = [why for _, why in risk_parts]
    prefs = spec.preferences
    cand.penalty_usd = (
        penalty_usd(q, prefs, cand.dest_weight)
        + prefs.risk_aversion * cand.risk * prefs.risk_unit_usd
    )
    return cand, None


def check(offer, direction, spec, origins, dests, kb):  # type: ignore[no-untyped-def]
    """Back-compat wrapper."""
    return build(offer, direction, spec, origins, dests, kb)


def _sanity(offer: AwardOffer, kb: KB) -> list[str]:
    it = offer.itinerary
    domestic = all(kb.is_domestic_us(s.origin, s.destination) for s in it.segments)
    band = kb.charts["plausibility"]["domestic" if domestic else "transpacific"][
        it.main_cabin.value
    ]
    flags = []
    if not (band[0] <= offer.miles <= band[1]):
        flags.append(
            f"price anomaly: {offer.miles:,} outside plausible band {band[0]:,}-{band[1]:,}"
        )
    if offer.program == "american":
        partner_long = [
            s for s in it.segments if s.operator != "AA" and s.duration.total_seconds() > 6 * 3600
        ]
        if partner_long:
            chart = _aa_partner_chart(kb, it.destination, it.main_cabin)
            if chart and offer.miles != chart:
                flags.append(
                    f"AA partner award {offer.miles:,} differs from KB chart {chart:,} "
                    "(chart may be stale, or mixed routing)"
                )
    if offer.taxes.amount > 1500:
        flags.append(f"high taxes/surcharges {offer.taxes}")
    return flags


def _aa_partner_chart(kb: KB, dest: str, cabin: Cabin) -> int | None:
    country = kb.airport(dest).country
    for zone in kb.charts["aa_partner"]["zones"].values():
        if country in zone["airports_in_countries"]:
            return zone.get(cabin.value)
    return None
