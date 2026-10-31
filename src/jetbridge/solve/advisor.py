"""The advisor: turns solver output into a decision.

1. Scores every solution on the dimensions a traveller actually trades off:
   points value (miles at the user's cpp, incl. card points transferred), cash,
   door-to-door hours, comfort deficit (hours below preferred cabin x $/h), and
   fragility (risk), *after* live verification.
2. Filters to the Pareto frontier (with tolerances, so $3 differences don't
   create fake trade-offs); dominated options are kept with "dominated by X".
3. Recommends the frontier option with the best risk-adjusted utility and
   explains every alternative as an explicit trade-off.
4. Sensitivity: because utility is linear in each cpp valuation, it computes
   the exact break-even valuation at which an alternative would win.
5. Emits an ordered action plan (verify -> transfer -> book scarcest first).

Everything is deterministic; Claude narrates it, never recomputes it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from jetbridge.kb import KB
from jetbridge.models.core import AwardOffer, Cabin
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.solve.allocator import Solution
from jetbridge.solve.valuation import CashIndex, cpp_achieved

VERIFY_RISK = {"confirmed": 0.0, "not re-checked": 0.1, "unverifiable": 0.15, "changed": 0.3}
TOL = {"points_usd": 25.0, "cash_usd": 25.0, "hours": 0.5, "comfort_usd": 50.0, "risk": 0.03}


@dataclass
class OptionAnalysis:
    index: int
    option_id: str
    solution: Solution
    points_by_currency: dict[str, int]
    points_usd: float
    cash_usd: float
    hours: float
    comfort_usd: float
    quality_usd: float  # all non-price penalties except risk
    risk: float
    risk_reasons: list[str]
    verification: str
    utility_usd: float  # lower is better; risk-adjusted
    pax_hours_below_pref: float = 0.0  # d(utility)/d(preferred-cabin $/h premium)
    redemptions: list[dict] = field(default_factory=list)
    dominated_by: str | None = None
    gone: bool = False
    equivalents: list[str] = field(
        default_factory=list
    )  # same on every dimension (within tolerance)

    @property
    def cabins(self) -> str:
        mix: dict[str, int] = defaultdict(int)
        for a in self.solution.assignments:
            mix[a.candidate.offer.itinerary.main_cabin.name.title()] += a.pax
        return " + ".join(f"{n}×{c}" for c, n in sorted(mix.items(), key=lambda x: -x[1]))


@dataclass
class Brief:
    recommendation: OptionAnalysis | None
    frontier: list[OptionAnalysis]
    alternatives: list[tuple[str, OptionAnalysis, str]]  # (role, option, trade-off text)
    dominated: list[OptionAnalysis]
    gone: list[OptionAnalysis]
    sensitivity: list[str]
    actions: list[str]
    insights: list[str]
    all: list[OptionAnalysis]

    def to_dict(self) -> dict:
        def od(o: OptionAnalysis) -> dict:
            return {
                "option_id": o.option_id,
                "label": o.solution.label,
                "cabins": o.cabins,
                "points_by_currency": o.points_by_currency,
                "points_value_usd": round(o.points_usd, 2),
                "cash_usd": round(o.cash_usd, 2),
                "hours": round(o.hours, 1),
                "comfort_deficit_usd": round(o.comfort_usd, 0),
                "risk": round(o.risk, 3),
                "risk_reasons": o.risk_reasons,
                "verification": o.verification,
                "utility_usd": round(o.utility_usd, 0),
                "redemptions": o.redemptions,
                "dominated_by": o.dominated_by,
                "equivalents": o.equivalents,
            }

        return {
            "recommendation": od(self.recommendation) if self.recommendation else None,
            "frontier": [od(o) for o in self.frontier],
            "alternatives": [
                {"role": r, "option": od(o), "tradeoff": t} for r, o, t in self.alternatives
            ],
            "dominated_count": len(self.dominated),
            "gone": [o.option_id for o in self.gone],
            "sensitivity": self.sensitivity,
            "actions": self.actions,
            "insights": self.insights,
        }


def _verification_status(v: dict) -> str:
    sts = {x.status for x in v.values()} if v else set()
    if not sts:
        return "not re-checked"
    for s in ("gone", "changed", "unverifiable"):
        if s in sts:
            return s
    return "confirmed"


def analyze_solutions(
    sols: list[Solution],
    option_ids: list[str],
    verified: dict[int, dict],
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    cash_index: CashIndex,
) -> list[OptionAnalysis]:
    prefs = spec.preferences
    out = []
    for i, sol in enumerate(sols):
        pts: dict[str, int] = defaultdict(int)
        for p, used in sol.own_used.items():
            pts[kb.programs[p].currency] += used
        for t in sol.transfers:
            pts[t.source] += t.points
        points_usd = sum(
            float(kb.cpp_for(c, profile.cpp_overrides)) * n / 100 for c, n in pts.items()
        )
        cash = float(sol.cash_usd)
        hours: dict[str, float] = defaultdict(float)
        comfort = quality = below = 0.0
        survive = 1.0
        reasons: list[str] = []
        seen: set[str] = set()
        redemptions = []
        for a in sol.assignments:
            c = a.candidate
            q = c.quality
            assert q is not None
            hours[c.direction] = max(hours[c.direction], q.total_hours)
            comfort += q.comfort_deficit_usd * a.pax
            below += a.pax * sum(
                h for cab, h in q.hours_by_cabin.items() if cab < spec.preferred_cabin
            )
            # penalty_usd includes the pre-verification risk term; remove it here
            quality += (c.penalty_usd - prefs.risk_aversion * c.risk * prefs.risk_unit_usd) * a.pax
            if c.id not in seen:
                seen.add(c.id)
                survive *= 1 - c.risk
                reasons += [r for r in c.risk_reasons if r not in reasons]
                if isinstance(c.offer, AwardOffer):
                    comp = cash_index.lookup(c)
                    val = cpp_achieved(c, comp) if comp else None
                    own_cpp = kb.cpp_for(
                        kb.programs[c.offer.program].currency, profile.cpp_overrides
                    )
                    redemptions.append(
                        {
                            "program": c.offer.program,
                            "miles_per_pax": c.offer.miles,
                            "cash_comparator_per_pax": str(comp.price_per_pax) if comp else None,
                            "basis": comp.basis if comp else None,
                            "cpp_achieved": str(val) if val is not None else None,
                            "cpp_valuation": str(own_cpp),
                            "verdict": _verdict(val, own_cpp),
                        }
                    )
        vstat = _verification_status(verified.get(i, {}))
        vr = VERIFY_RISK.get(vstat, 0.0)
        if vr:
            survive *= 1 - vr
            reasons.append(f"verification: {vstat}")
        multi_ticket = len({a.candidate.id for a in sol.assignments}) > len(
            {a.candidate.direction for a in sol.assignments}
        )
        if multi_ticket:
            survive *= 0.95
            reasons.append("party split across separate award tickets")
        risk = 1 - survive
        n = spec.passengers
        utility = points_usd + cash + quality + prefs.risk_aversion * risk * prefs.risk_unit_usd * n
        out.append(
            OptionAnalysis(
                index=i,
                option_id=option_ids[i] if i < len(option_ids) else f"opt_{i}",
                solution=sol,
                points_by_currency=dict(pts),
                points_usd=points_usd,
                cash_usd=cash,
                hours=sum(hours.values()),
                comfort_usd=comfort,
                quality_usd=quality,
                risk=risk,
                risk_reasons=reasons,
                verification=vstat,
                utility_usd=utility,
                pax_hours_below_pref=below,
                redemptions=redemptions,
                gone=vstat == "gone",
            )
        )
    return out


def _verdict(val: Decimal | None, own: Decimal) -> str:
    if val is None:
        return "no cash comparator found"
    if val >= own:
        return "good redemption (beats your valuation)"
    if val >= own * Decimal("0.8"):
        return "fair redemption (slightly below your valuation)"
    return "poor redemption: paying cash may be the better use of your miles"


def _dims(o: OptionAnalysis) -> dict[str, float]:
    return {
        "points_usd": o.points_usd,
        "cash_usd": o.cash_usd,
        "hours": o.hours,
        "comfort_usd": o.comfort_usd,
        "risk": o.risk,
    }


def dominates(a: OptionAnalysis, b: OptionAnalysis) -> bool:
    da, db = _dims(a), _dims(b)
    no_worse = all(da[k] <= db[k] + TOL[k] for k in da)
    strictly = any(da[k] < db[k] - TOL[k] for k in da)
    return no_worse and strictly


def equivalent(a: OptionAnalysis, b: OptionAnalysis) -> bool:
    da, db = _dims(a), _dims(b)
    return all(abs(da[k] - db[k]) <= TOL[k] for k in da) and a.cabins == b.cabins


def pareto(opts: list[OptionAnalysis]) -> tuple[list[OptionAnalysis], list[OptionAnalysis]]:
    """Non-dominated set. Options equal (within tolerance) on every dimension are
    collapsed: the best-utility one stays on the frontier and lists the others in
    ``equivalents`` (different flights/dates, same decision value)."""
    live = sorted((o for o in opts if not o.gone), key=lambda o: o.utility_usd)
    front, dom = [], []
    for o in live:
        better = [p for p in live if p is not o and dominates(p, o)]
        if better:
            o.dominated_by = min(better, key=lambda p: p.utility_usd).option_id
            dom.append(o)
            continue
        twin = next((f for f in front if equivalent(f, o)), None)
        if twin is not None:
            twin.equivalents.append(o.option_id)
            o.dominated_by = f"equivalent to {twin.option_id}"
            dom.append(o)
        else:
            front.append(o)
    return front, dom


def _fmt_pts(d: dict[str, int], kb: KB) -> str:
    return " + ".join(f"{n:,} {kb.currencies.get(c, c)}" for c, n in d.items()) or "no points"


def tradeoff(alt: OptionAnalysis, rec: OptionAnalysis, kb: KB) -> str:
    parts = []
    allc = set(alt.points_by_currency) | set(rec.points_by_currency)
    for c in sorted(allc):
        d = alt.points_by_currency.get(c, 0) - rec.points_by_currency.get(c, 0)
        if d:
            parts.append(f"{'+' if d > 0 else '−'}{abs(d):,} {kb.currencies.get(c, c)}")
    dc = alt.cash_usd - rec.cash_usd
    if abs(dc) >= 1:
        parts.append(f"{'+' if dc > 0 else '−'}${abs(dc):,.0f} cash")
    dh = alt.hours - rec.hours
    if abs(dh) >= 0.25:
        parts.append(f"{'+' if dh > 0 else '−'}{abs(dh):.1f}h travel")
    if alt.cabins != rec.cabins:
        parts.append(f"cabins {alt.cabins} (vs {rec.cabins})")
    dr = alt.risk - rec.risk
    if abs(dr) >= 0.05:
        parts.append(f"{'higher' if dr > 0 else 'lower'} risk ({alt.risk:.0%} vs {rec.risk:.0%})")
    du = alt.utility_usd - rec.utility_usd
    parts.append(f"net ${du:+,.0f} vs recommendation on your preferences")
    return "; ".join(parts)


def sensitivity(
    rec: OptionAnalysis, alts: list[OptionAnalysis], profile: Profile, kb: KB
) -> list[str]:
    """Exact break-even cpp per currency (utility is linear in each valuation).

    Ordered by fragility: a break-even close to the current valuation means the
    recommendation hinges on a valuation judgement and deserves a second look."""
    found: list[tuple[float, str]] = []
    for alt in alts:
        gap = alt.utility_usd - rec.utility_usd  # >= 0 by construction (rec is best)
        for c in sorted(set(alt.points_by_currency) | set(rec.points_by_currency)):
            dp = alt.points_by_currency.get(c, 0) - rec.points_by_currency.get(c, 0)
            if dp == 0:
                continue
            cur = float(kb.cpp_for(c, profile.cpp_overrides))
            be = cur - gap * 100 / dp  # U_alt - U_rec changes by dp * delta_cpp / 100 dollars
            if not 0.2 <= be <= 5.0:
                continue
            rel = abs(be - cur) / cur
            direction = "below" if be < cur else "above"
            tag = "FRAGILE: " if rel <= 0.10 else ""
            found.append(
                (
                    rel,
                    f"{tag}{alt.option_id} would beat {rec.option_id} if you valued "
                    f"{kb.currencies.get(c, c)} {direction} {be:.2f}¢ (currently {cur:.2f}¢)",
                )
            )
    found.sort(key=lambda t: t[0])
    return [line for _, line in found[:8]]


def cabin_sensitivity(
    rec: OptionAnalysis, alts: list[OptionAnalysis], spec: SearchSpec
) -> list[str]:
    """Break-even on the preferred cabin's $/flown-hour premium. Utility is linear in it
    with slope = passenger-hours flown below the preferred cabin (exact for options whose
    below-preferred segments are a single cabin; for mixed W/Y it is the slope of the
    preferred-cabin premium holding the lower cabins' premiums fixed)."""
    prem = spec.preferences.cabin_value(spec.preferred_cabin)
    name = spec.preferred_cabin.name.lower()
    out: list[tuple[float, str]] = []
    for alt in alts:
        if alt.cabins == rec.cabins:
            continue  # same cabin mix: the cabin premium is not the real lever
        slope = alt.pax_hours_below_pref - rec.pax_hours_below_pref
        if abs(slope) < 1e-6:
            continue
        gap = alt.utility_usd - rec.utility_usd
        be = prem - gap / slope
        if be <= 0 or be > prem * 4:
            continue
        rel = abs(be - prem) / prem
        side = "less" if be < prem else "more"
        tag = "FRAGILE: " if rel <= 0.10 else ""
        out.append(
            (
                rel,
                f"{tag}{alt.option_id} ({alt.cabins}) would beat {rec.option_id} ({rec.cabins}) if {name} "
                f"is worth {side} than ${be:,.0f} per flown hour to you (currently ${prem:,.0f}/h)",
            )
        )
    out.sort(key=lambda t: t[0])
    return [line for _, line in out[:4]]


def actions(rec: OptionAnalysis, kb: KB, spec: SearchSpec) -> list[str]:
    sol = rec.solution
    steps = []
    if rec.verification != "confirmed":
        steps.append(
            f"Re-check {rec.option_id} live right before acting (status: {rec.verification}); "
            "award space can disappear within minutes."
        )
    else:
        steps.append("Space was confirmed live minutes ago; act soon, it can still disappear.")
    for t in sol.transfers:
        tr = next(
            (x for x in kb.transfers if x.source == t.source and x.program == t.program), None
        )
        steps.append(
            f"Transfer {t.points:,} {kb.currencies[t.source]} → {t.miles:,} {kb.programs[t.program].name} "
            f"({tr.time if tr else '?'} transfer; IRREVERSIBLE — only after confirming space"
            f"{'; ratio flagged verify in KB' if tr and tr.confidence == 'verify' else ''})."
        )

    def fragility(a) -> tuple:  # type: ignore[no-untyped-def]
        s = a.candidate.offer.seats
        margin = (s.count - a.pax) if (s and s.kind == "exact") else 99
        return (margin, -a.candidate.offer.itinerary.duration.total_seconds())

    for a in sorted(sol.assignments, key=fragility):
        c = a.candidate
        o = c.offer
        it = o.itinerary
        what = (
            f"{o.miles:,} {kb.programs[o.program].name} miles + {o.taxes} per person"
            if isinstance(o, AwardOffer)
            else f"cash {o.price} per person"
        )
        seat = o.seats
        scarce = (
            f" — only {seat.count} seat(s) seen, book this first"
            if seat and seat.kind == "exact" and seat.count - a.pax <= 1
            else ""
        )
        steps.append(
            f"Book {a.pax} passenger(s) {it.origin}→{it.destination} "
            f"{it.departs_at.strftime('%a %b %d %H:%M')} "
            f"({' '.join(s.flight_code for s in it.segments)}): {what}{scarce}."
        )
        for x in c.positioning:
            xi = x.itinerary
            steps.append(
                f"Then buy positioning {xi.origin}→{xi.destination} "
                f"{xi.departs_at.strftime('%a %b %d %H:%M')} ({' '.join(s.flight_code for s in xi.segments)}) "
                f"for ~{x.price}/person on a separate ticket; it arrives with "
                f"{_buffer_minutes(c)} min to spare. Prefer a changeable fare."
            )
    if len({a.candidate.id for a in sol.assignments}) > 1:
        steps.append(
            "The party is on more than one award ticket: after booking, ask the airline to link "
            "the reservations so seats and irregular-operations rebooking stay together."
        )
    for r in rec.redemptions:
        if r["verdict"].startswith("poor"):
            steps.append(
                f"Consider paying cash for the {r['program']} portion: it yields {r['cpp_achieved']}¢/mile "
                f"vs your {r['cpp_valuation']}¢ valuation (cash comparator {r['basis']})."
            )
    return steps


def _buffer_minutes(c) -> int:  # type: ignore[no-untyped-def]
    q = c.quality
    seps = [lv.minutes for lv in q.layovers if lv.separate_ticket] if q else []
    return min(seps) if seps else 0


def insights(opts: list[OptionAnalysis], spec: SearchSpec, kb: KB) -> list[str]:
    out = []
    live = [o for o in opts if not o.gone]
    pref = spec.preferred_cabin
    full_pref = [
        o
        for o in live
        if all(a.candidate.offer.itinerary.main_cabin >= pref for a in o.solution.assignments)
    ]
    if not full_pref:
        out.append(
            f"No option seats all {spec.passengers} passengers in {pref.name.title()} under your constraints; "
            "the recommendations below mix cabins or use a lower cabin."
        )
    composed = [o for o in live if any(a.candidate.composed for a in o.solution.assignments)]
    if composed:
        best_c = min(composed, key=lambda o: o.utility_usd)
        out.append(
            f"Positioning on a separate ticket opens {len(composed)} option(s); best is {best_c.option_id} "
            f"({best_c.cabins})."
        )
    poor = [r for o in live[:5] for r in o.redemptions if r["verdict"].startswith("poor")]
    if poor:
        out.append(
            f"{len(poor)} of the top redemptions are below your valuation; cash may be competitive for those."
        )
    return out


def advise(
    sols: list[Solution],
    option_ids: list[str],
    verified: dict[int, dict],
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    cash_index: CashIndex,
) -> Brief:
    opts = analyze_solutions(sols, option_ids, verified, spec, profile, kb, cash_index)
    front, dom = pareto(opts)
    gone = [o for o in opts if o.gone]
    rec = front[0] if front else None
    alts: list[tuple[str, OptionAnalysis, str]] = []
    if rec:
        roles = [
            ("fewest points", lambda o: (o.points_usd, o.utility_usd)),
            ("least cash", lambda o: (o.cash_usd, o.utility_usd)),
            ("fastest", lambda o: (o.hours, o.utility_usd)),
            ("most comfortable", lambda o: (o.comfort_usd, o.utility_usd)),
            ("lowest risk", lambda o: (o.risk, o.utility_usd)),
        ]
        used = {rec.option_id}
        for role, key in roles:
            best = min(front, key=key)
            if best.option_id not in used and key(best)[0] < key(rec)[0] - 1e-9:
                used.add(best.option_id)
                alts.append((role, best, tradeoff(best, rec, kb)))
        for o in front:
            if o.option_id not in used and len(alts) < 6:
                used.add(o.option_id)
                alts.append(("also on the frontier", o, tradeoff(o, rec, kb)))
    return Brief(
        recommendation=rec,
        frontier=front,
        alternatives=alts,
        dominated=dom,
        gone=gone,
        sensitivity=(
            cabin_sensitivity(rec, [a for _, a, _ in alts], spec)
            + sensitivity(rec, [a for _, a, _ in alts], profile, kb)
        )
        if rec
        else [],
        actions=actions(rec, kb, spec) if rec else [],
        insights=insights(opts, spec, kb),
        all=opts,
    )


__all__ = ["Brief", "Cabin", "OptionAnalysis", "advise", "dominates", "pareto"]
