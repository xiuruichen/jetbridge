"""Cash-vs-points reasoning.

For each award candidate we find what the same trip would cost in cash:
  * ``exact``      — Google Flights priced the very same flights in the same cabin,
  * ``comparable`` — cheapest cash fare, same direction/date/destination/cabin,
and compute the redemption value actually achieved:

    cpp_achieved = (cash_equivalent - award_taxes) / miles * 100

If cpp_achieved is below what the user values those miles at, paying cash is
the better use of resources — the optimizer already prices this (miles at cpp
vs cash), and the advisor *explains* it.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from jetbridge.models.core import AwardOffer, CashOffer
from jetbridge.solve.eligibility import Candidate


@dataclass(frozen=True)
class CashComparator:
    price_per_pax: Decimal
    basis: str  # exact|comparable
    offer_id: str
    flights: str


class CashIndex:
    def __init__(self, cands: list[Candidate]):
        self.exact: dict[tuple[str, str], CashOffer] = {}
        self.comparable: dict[tuple[str, str, str, str], CashOffer] = {}
        for c in cands:
            o = c.offer
            if not isinstance(o, CashOffer) or c.composed:
                continue
            it = o.itinerary
            ke = (it.flight_key, it.main_cabin.value)
            if ke not in self.exact or o.price.amount < self.exact[ke].price.amount:
                self.exact[ke] = o
            end = it.destination if c.direction == "out" else it.origin
            kc = (c.direction, it.departs_at.date().isoformat(), end, it.main_cabin.value)
            if kc not in self.comparable or o.price.amount < self.comparable[kc].price.amount:
                self.comparable[kc] = o

    def lookup(self, c: Candidate) -> CashComparator | None:
        it = c.itinerary
        cab = c.offer.itinerary.main_cabin.value
        hit = self.exact.get((it.flight_key, cab))
        basis = "exact"
        if hit is None:
            end = it.destination if c.direction == "out" else it.origin
            hit = self.comparable.get((c.direction, it.departs_at.date().isoformat(), end, cab))
            basis = "comparable"
        if hit is None:
            return None
        return CashComparator(
            hit.price.amount,
            basis,
            hit.offer_id,
            " ".join(s.flight_code for s in hit.itinerary.segments),
        )

    def __len__(self) -> int:
        return len(self.exact)


def cpp_achieved(c: Candidate, comp: CashComparator) -> Decimal | None:
    o = c.offer
    if not isinstance(o, AwardOffer) or o.miles <= 0:
        return None
    # The comparator prices the whole trip from the origin; the award trip also pays
    # taxes and any cash positioning legs. Value created by the miles = the rest.
    extras = sum((x.price.amount for x in c.positioning), Decimal(0))
    net = comp.price_per_pax - o.taxes.amount - extras
    return (net * 100 / o.miles).quantize(Decimal("0.01"))
