"""Adaptive refinement: evidence-driven follow-up searches + positioning composition.

After the first pass the engine knows where the gaps are. It spends a bounded
budget on the searches most likely to change the answer:

1. split completion  — an airline shows s (1 < s < N) seats in the preferred
   cabin, but its price depends on party size: price an exact s-pax booking so
   the solver can combine it soundly with another fare.
2. gateway long-haul — for (program, destination, date) cells with no
   preferred-cabin space for the whole party from the home airport (often the
   *domestic connection* is what's missing), search that program's long-haul
   gateways directly.
3. positioning       — for gateway departures that showed space, price cash
   positioning flights (same day and the evening before) home<->gateway.

Every follow-up query carries a human-readable reason that lands in the ledger.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import timedelta

from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.types import Query
from jetbridge.kb import KB
from jetbridge.models.core import AwardOffer, Cabin, CashOffer
from jetbridge.models.spec import SearchSpec
from jetbridge.plan.planner import PlannedQuery
from jetbridge.solve.eligibility import Candidate


@dataclass
class RefineQuery:
    pq: PlannedQuery
    reason: str


def _home(spec: SearchSpec, kb: KB) -> list[str]:
    out: list[str] = []
    for o in [*spec.origins, *spec.nearby_origins]:
        out += [a for a in kb.expand(o) if a not in out]
    return out


def phase_a(
    spec: SearchSpec,
    kb: KB,
    adapters: dict[str, Adapter],
    cands: list[Candidate],
    dests: dict[str, float],
    done_keys: set[str],
    budget: int,
) -> list[RefineQuery]:
    out: list[RefineQuery] = []
    n = spec.passengers
    pref = spec.preferred_cabin
    award_adapters = [a for a in adapters.values() if a.capabilities.award]

    # 1. split completion ----------------------------------------------------
    seen_split: set[str] = set()
    for c in cands:
        o = c.offer
        if not isinstance(o, AwardOffer) or c.composed:
            continue
        a = adapters.get(o.provenance.adapter)
        if a is None or a.price_independent_of_pax:
            continue
        s = o.seats
        if o.itinerary.main_cabin >= pref and s.kind == "exact" and 1 < s.count < n:
            it = o.itinerary
            q = Query(a.id, "day", it.origin, it.destination, it.departs_at.date(), s.count)
            key = q.key + f":{c.direction}"
            if key in done_keys or key in seen_split:
                continue
            seen_split.add(key)
            out.append(
                RefineQuery(
                    PlannedQuery(q, "refine-split", c.direction, priority=3.0),
                    f"{o.program} shows only {s.count} {pref.name.lower()} seats on "
                    f"{' '.join(x.flight_code for x in it.segments)}; pricing a {s.count}-passenger booking",
                )
            )

    # 2. gateway long-haul ---------------------------------------------------
    if not spec.include_positioning:
        return out[:budget]
    home = set(_home(spec, kb))
    covered: dict[tuple[str, str, str, str], bool] = defaultdict(bool)
    best_price: dict[tuple[str, str, str, str], int] = {}
    for c in cands:
        o = c.offer
        if not isinstance(o, AwardOffer) or c.composed:
            continue
        it = o.itinerary
        end = it.destination if c.direction == "out" else it.origin
        key = (o.program, c.direction, end, it.departs_at.date().isoformat())
        if it.main_cabin >= pref:
            seats_ok = o.seats.count >= n or o.provenance.request_pax >= n
            covered[key] |= seats_ok
            best_price[key] = min(best_price.get(key, 10**9), o.miles)
    # program-level typical price for "overpriced" detection
    typical: dict[tuple[str, str, str], int] = {}
    for (p, d, e, _day), m in best_price.items():
        k = (p, d, e)
        typical[k] = min(typical.get(k, 10**9), m)

    windows = [("out", spec.outbound.days())]
    if spec.inbound:
        windows.append(("in", spec.inbound.days()))
    scored: list[tuple[float, RefineQuery]] = []
    for a in award_adapters:
        prog = kb.programs.get(a.program or "")
        if prog is None:
            continue
        for direction, days in windows:
            for dest, w in dests.items():
                if w < 0.9:
                    continue
                for day in days:
                    key = (prog.id, direction, dest, day.isoformat())
                    gap = not covered.get(key, False)
                    pricey = key in best_price and best_price[key] > 1.3 * typical.get(
                        key[:3], best_price[key]
                    )
                    if not (gap or pricey):
                        continue
                    why = (
                        f"no {pref.name.lower()} space for {n} via {prog.id} from home on {day}"
                        if gap
                        else f"{prog.id} {pref.name.lower()} from home is {best_price[key]:,}/pax on {day} "
                        f"(>30% above its best {typical[key[:3]]:,})"
                    )
                    for g in prog.gateways:
                        if g in home:
                            continue
                        o, d = (g, dest) if direction == "out" else (dest, g)
                        q = Query(a.id, "day", o, d, day, n)
                        qk = q.key + f":{direction}"
                        if qk in done_keys:
                            continue
                        rq = RefineQuery(
                            PlannedQuery(q, "refine-gateway", direction, priority=2.5 + w),
                            f"{why}; checking {prog.id} long-haul space from gateway {g}",
                        )
                        scored.append(((2.0 if gap else 1.0) * w, rq))
    scored.sort(key=lambda t: -t[0])
    out += [rq for _, rq in scored]
    return out[:budget]


def phase_b(
    spec: SearchSpec,
    kb: KB,
    adapters: dict[str, Adapter],
    gateway_offers: list[tuple[str, AwardOffer]],
    done_keys: set[str],
    budget: int,
    through: list[Candidate] | None = None,
) -> list[RefineQuery]:
    """Positioning cash queries, only where a gateway award can actually compete.

    A gateway award is worth positioning for when it is in the preferred cabin
    and the through-search had no preferred-cabin option for that day, or when it
    is cheaper (miles) than the best through option in the same cabin/day/end.
    Same-day positioning is searched first; the evening before only when the
    long-haul departs too early for a same-day connection with the separate-ticket
    buffer (arrival-side mirror for return trips). Ranked by value; capped by budget.
    """
    if "google_flights" not in adapters or budget <= 0:
        return []
    home = _home(spec, kb)[:1]  # position from the primary home airport
    pref = spec.preferred_cabin
    best_through: dict[tuple[str, str, str, str], int] = {}
    for c in through or []:
        o = c.offer
        if not isinstance(o, AwardOffer) or c.composed:
            continue
        it = o.itinerary
        end = it.destination if c.direction == "out" else it.origin
        k = (c.direction, end, it.departs_at.date().isoformat(), it.main_cabin.value)
        best_through[k] = min(best_through.get(k, 10**9), o.miles)

    scored: dict[tuple[str, str, str], tuple[float, str, str]] = {}
    for direction, o in gateway_offers:
        it = o.itinerary
        cab = it.main_cabin
        if cab < spec.min_cabin:
            continue
        end = it.destination if direction == "out" else it.origin
        day = it.departs_at.date().isoformat()
        thr = best_through.get((direction, end, day, cab.value))
        if cab >= pref and thr is None:
            value, why = 3.0, f"{cab.name.lower()} space from gateway; none from home that day"
        elif thr is not None and o.miles < thr:
            value, why = 1.0 + (thr - o.miles) / thr, f"{o.miles:,} vs best {thr:,} from home"
        else:
            continue  # cannot beat what home already offers
        if direction == "out":
            g = it.origin
            # same-day works if the long-haul leaves late enough for a morning hop + buffer
            days = [it.departs_at.date()]
            if it.departs_at.hour < 11:
                days.append(it.departs_at.date() - timedelta(days=1))
            keys = [(h, g, d.isoformat()) for h in home for d in days]
        else:
            g = it.destination
            days = [it.arrives_at.date()]
            if it.arrives_at.hour >= 18:
                days.append(it.arrives_at.date() + timedelta(days=1))
            keys = [(g, h, d.isoformat()) for h in home for d in days]
        for k in keys:
            prev = scored.get(k)
            if prev is None or value > prev[0]:
                scored[k] = (value, direction, why)
    out = []
    from datetime import date as _date

    for (o, d, day), (value, direction, why) in sorted(scored.items(), key=lambda kv: -kv[1][0]):
        q = Query(
            "google_flights", "day", o, d, _date.fromisoformat(day), spec.passengers, Cabin.ECONOMY
        )
        qk = q.key + f":{direction}"
        if qk in done_keys:
            continue
        out.append(
            RefineQuery(
                PlannedQuery(q, "refine-position", direction, priority=2.0 + value),
                f"cash positioning {o}→{d} on {day}: {why}",
            )
        )
    return out[:budget]


def compose(
    spec: SearchSpec,
    kb: KB,
    gateway_offers: list[tuple[str, AwardOffer]],
    positioning: list[CashOffer],
    *,
    per_longhaul: int = 3,
) -> list[tuple[str, AwardOffer, tuple[CashOffer, ...], tuple[CashOffer, ...]]]:
    """Pair gateway awards with feasible cash positioning legs.

    Returns (direction, award, extras_before, extras_after). Feasibility here is
    coarse (ordering + max 30h gap); ``eligibility.build`` enforces the
    separate-ticket buffer, airport changes and all hard constraints.
    """
    home = set(_home(spec, kb))
    buffer = timedelta(minutes=kb.mct_minutes("separate_ticket_buffer"))
    by_route: dict[tuple[str, str], list[CashOffer]] = defaultdict(list)
    for p in positioning:
        it = p.itinerary
        by_route[(it.origin, it.destination)].append(p)
    out = []
    for direction, aw in gateway_offers:
        it = aw.itinerary
        opts: list[tuple[float, CashOffer]] = []
        if direction == "out":
            g = it.origin
            for h in home:
                for p in by_route.get((h, g), []):
                    gap = it.departs_at - p.itinerary.arrives_at
                    if buffer <= gap <= timedelta(hours=30):
                        opts.append((float(p.price.amount) + gap.total_seconds() / 3600 * 5, p))
        else:
            g = it.destination
            for h in home:
                for p in by_route.get((g, h), []):
                    gap = p.itinerary.departs_at - it.arrives_at
                    if buffer <= gap <= timedelta(hours=30):
                        opts.append((float(p.price.amount) + gap.total_seconds() / 3600 * 5, p))
        opts.sort(key=lambda t: t[0])
        for _, p in opts[:per_longhaul]:
            out.append(
                (direction, aw, (p,), ()) if direction == "out" else (direction, aw, (), (p,))
            )
    return out
