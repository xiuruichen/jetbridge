"""Tests for the optimizing/reasoning layer: quality, composition, Pareto, sensitivity."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from jetbridge.kb import load_kb
from jetbridge.models.core import (
    AwardOffer,
    Cabin,
    CashOffer,
    DateWindow,
    Itinerary,
    Money,
    Provenance,
    SeatInfo,
    Segment,
)
from jetbridge.models.spec import DestinationPref, Profile, SearchSpec
from jetbridge.plan.refine import compose
from jetbridge.solve.advisor import advise, dominates, pareto
from jetbridge.solve.allocator import Allocator
from jetbridge.solve.eligibility import build
from jetbridge.solve.quality import analyze
from jetbridge.solve.valuation import CashIndex

KB = load_kb()
ET = timezone(timedelta(hours=-4))
CT = timezone(timedelta(hours=-5))
KST = timezone(timedelta(hours=9))


def seg(code, fn, o, d, dep, hours, cabin, tz_arr=None):
    arr = (dep + timedelta(hours=hours)).astimezone(tz_arr or dep.tzinfo)
    return Segment(
        marketing_carrier=code,
        flight_number=fn,
        origin=o,
        destination=d,
        departs_at=dep,
        arrives_at=arr,
        cabin=cabin,
    )


def prov(adapter, pax=3, hours_ago=0):
    return Provenance(
        adapter=adapter,
        tier="replay",
        fetch_id="f",
        observed_at=datetime.now(UTC) - timedelta(hours=hours_ago),
        request_pax=pax,
    )


def award(program, segs, miles, seats=9, kind="lower_bound", taxes=5.6, pax=3):
    return AwardOffer(
        program=program,
        itinerary=Itinerary(segments=tuple(segs)),
        miles=miles,
        taxes=Money.usd(taxes),
        seats=SeatInfo(kind=kind, count=seats),
        provenance=prov(program, pax),
    )


def cash(segs, price, pax=3):
    return CashOffer(
        source="google_flights",
        itinerary=Itinerary(segments=tuple(segs)),
        price=Money.usd(price),
        provenance=prov("google_flights", pax),
    )


SPEC = SearchSpec(
    origins=["CVG"],
    destinations=[DestinationPref(code="ICN")],
    outbound=DateWindow(start=date(2027, 5, 27), end=date(2027, 5, 30)),
    passengers=3,
    preferred_cabin=Cabin.BUSINESS,
)
ORIGINS, DESTS = ["CVG"], {"ICN": 1.0}


# ---------------------------------------------------------------- quality
def test_comfort_deficit_scales_with_hours_flown():
    p = SPEC.preferences
    short = Itinerary(
        segments=(
            seg("DL", "1", "CVG", "DTW", datetime(2027, 5, 28, 8, tzinfo=ET), 1.2, Cabin.ECONOMY),
        )
    )
    long = Itinerary(
        segments=(
            seg(
                "DL",
                "2",
                "DTW",
                "ICN",
                datetime(2027, 5, 28, 12, tzinfo=ET),
                14.5,
                Cabin.ECONOMY,
                KST,
            ),
        )
    )
    qs, ql = analyze(short, KB, p, Cabin.BUSINESS), analyze(long, KB, p, Cabin.BUSINESS)
    assert qs.comfort_deficit_usd == pytest.approx(1.2 * 130)
    assert ql.comfort_deficit_usd == pytest.approx(14.5 * 130)
    assert ql.comfort_deficit_usd > 10 * qs.comfort_deficit_usd


def test_overnight_and_tight_connections_flagged():
    p = SPEC.preferences
    a = seg("DL", "1", "CVG", "ATL", datetime(2027, 5, 28, 20, tzinfo=ET), 1.5, Cabin.BUSINESS)
    b = seg("DL", "27", "ATL", "ICN", datetime(2027, 5, 29, 9, tzinfo=ET), 15, Cabin.BUSINESS, KST)
    q = analyze(Itinerary(segments=(a, b)), KB, p, Cabin.BUSINESS)
    assert q.layovers[0].overnight and any("overnight" in f for f in q.flags)
    c = seg("DL", "3", "ATL", "ICN", a.arrives_at + timedelta(minutes=50), 15, Cabin.BUSINESS, KST)
    q2 = analyze(Itinerary(segments=(a, c)), KB, p, Cabin.BUSINESS)
    assert q2.layovers[0].tight  # 50 min < 90 intl MCT


# ---------------------------------------------------------------- composition
def _gateway_award(dep_hour=12):
    return award(
        "delta",
        [
            seg(
                "DL",
                "159",
                "DTW",
                "ICN",
                datetime(2027, 5, 28, dep_hour, 45, tzinfo=ET),
                14.7,
                Cabin.BUSINESS,
                KST,
            )
        ],
        150_000,
        seats=4,
        kind="exact",
    )


def _positioning(dep_hour, minutes=75):
    fn = str(5300 + dep_hour)  # a flight number is unique per day
    return cash(
        [
            seg(
                "DL",
                fn,
                "CVG",
                "DTW",
                datetime(2027, 5, 28, dep_hour, tzinfo=ET),
                minutes / 60,
                Cabin.ECONOMY,
            )
        ],
        129,
    )


def test_compose_respects_separate_ticket_buffer():
    aw = _gateway_award()
    early = _positioning(7)  # arrives 08:15, 4.5h before 12:45
    late = _positioning(10, 60)  # arrives 11:00, 1h45m before -> below 180 min buffer
    combos = compose(SPEC, KB, [("out", aw)], [early, late])
    used = {c[2][0].offer_id for c in combos}
    assert early.offer_id in used and late.offer_id not in used
    direction, a, before, after = combos[0]
    cand, why = build(a, direction, SPEC, ORIGINS, DESTS, KB, extras=before, extras_after=after)
    assert cand is not None, why
    assert cand.itinerary.origin == "CVG" and cand.composed
    assert cand.cash_per_pax == Decimal("134.60")  # 129 positioning + 5.60 award taxes
    assert any("separate tickets" in r for r in cand.risk_reasons)
    assert cand.quality.layovers[0].separate_ticket


def test_build_rejects_positioning_below_buffer():
    cand, why = build(
        _gateway_award(), "out", SPEC, ORIGINS, DESTS, KB, extras=(_positioning(10, 60),)
    )
    assert cand is None and "separate-ticket buffer" in why


# ---------------------------------------------------------------- advisor
def _through(program, miles, hours, cabin=Cabin.BUSINESS, seats=9, kind="lower_bound", fn="1"):
    a = seg(
        "XX" if program == "x" else "DL",
        fn,
        "CVG",
        "ATL",
        datetime(2027, 5, 28, 8, tzinfo=ET),
        1.5,
        cabin,
    )
    b = seg(
        "DL", str(int(fn) + 100), "ATL", "ICN", a.arrives_at + timedelta(hours=2), hours, cabin, KST
    )
    return award(program, [a, b], miles, seats=seats, kind=kind)


def _solve(offers, profile=None, spec=SPEC):
    cands = [build(o, "out", spec, ORIGINS, DESTS, KB)[0] for o in offers]
    assert all(cands)
    prof = profile or Profile()
    sols = Allocator(spec, prof, KB, {"united": True}).solve(cands, k=6)
    ids = [f"o{i}" for i in range(len(sols))]
    return advise(sols, ids, {}, spec, prof, KB, CashIndex(cands)), sols


def test_dominated_option_is_filtered_with_reason():
    good = _through("delta", 100_000, 14, fn="1")
    worse = _through("delta", 140_000, 16, fn="2")  # more miles AND slower
    brief, _ = _solve([good, worse])
    assert brief.recommendation is not None
    dom = {o.option_id: o.dominated_by for o in brief.dominated}
    assert dom and all(v == brief.recommendation.option_id for v in dom.values())
    assert all(dominates(brief.recommendation, o) for o in brief.dominated)


def test_pareto_tolerance_ignores_trivial_differences():
    brief, _ = _solve(
        [_through("delta", 100_000, 14, fn="1"), _through("delta", 100_000, 14, fn="3")]
    )
    a, b = brief.all[0], brief.all[1]
    assert not dominates(a, b) and not dominates(b, a)  # identical within tolerance
    for o in brief.all:
        o.equivalents, o.dominated_by = [], None
    front, dom = pareto(brief.all)
    assert len(front) == 1 and front[0].equivalents == [dom[0].option_id]  # collapsed twins


def test_sensitivity_break_even_is_exact():
    """Delta (cheap miles, slow) vs United (more miles, fast). The reported break-even
    valuation must actually flip the recommendation."""
    d = _through("delta", 100_000, 16, fn="1")
    u = award(
        "united",
        [
            seg(
                "UA",
                "893",
                "CVG",
                "ICN",
                datetime(2027, 5, 28, 9, tzinfo=ET),
                15.5,
                Cabin.BUSINESS,
                KST,
            )
        ],
        130_000,
    )
    brief, _ = _solve([d, u])
    rec = brief.recommendation
    assert rec is not None and brief.sensitivity, brief.sensitivity
    line = brief.sensitivity[0]
    m = re.search(r"if you valued (.+?) (below|above) ([\d.]+)¢", line)
    assert m, line
    name, side, be = m.group(1), m.group(2), float(m.group(3))
    cur = next(c for c, n in KB.currencies.items() if n == name)
    eps = 0.02
    flipped = be - eps if side == "below" else be + eps
    held = be + eps if side == "below" else be - eps
    brief_f, _ = _solve([d, u], Profile(cpp_overrides={cur: Decimal(str(round(flipped, 4)))}))
    brief_h, _ = _solve([d, u], Profile(cpp_overrides={cur: Decimal(str(round(held, 4)))}))

    def programs(b):
        return set(b.recommendation.solution.miles_by_program)

    assert programs(brief_h) == set(rec.solution.miles_by_program)
    assert programs(brief_f) != set(rec.solution.miles_by_program)


def test_last_seats_raise_risk_and_actions_say_book_first():
    tight = _through("delta", 100_000, 14, seats=3, kind="exact", fn="1")
    brief, _ = _solve([tight])
    rec = brief.recommendation
    assert rec.risk > 0.3 and any("last 3 seat" in r for r in rec.risk_reasons)
    assert any("book this first" in a for a in brief.actions)


def test_redemption_value_vs_cash():
    aw = _through("delta", 100_000, 14, fn="1")
    same_flights_cash = cash(list(aw.itinerary.segments), 3000)
    cands = [build(o, "out", SPEC, ORIGINS, DESTS, KB)[0] for o in (aw, same_flights_cash)]
    idx = CashIndex(cands)
    comp = idx.lookup(cands[0])
    assert comp is not None and comp.basis == "exact" and comp.price_per_pax == Decimal("3000")
    from jetbridge.solve.valuation import cpp_achieved

    assert cpp_achieved(cands[0], comp) == Decimal("2.99")  # (3000 - 5.60) / 100000 * 100


def test_cabin_premium_break_even_is_exact():
    """Business (expensive) vs economy (cheap): the reported $/h break-even flips it."""
    from jetbridge.models.spec import Preferences

    j = _through("delta", 300_000, 14, Cabin.BUSINESS, fn="1")
    y = _through("delta", 90_000, 14, Cabin.ECONOMY, fn="2")

    def run(prem):
        prefs = Preferences(
            cabin_premium_usd_per_hour={
                Cabin.ECONOMY: 0,
                Cabin.PREMIUM: 45,
                Cabin.BUSINESS: prem,
                Cabin.FIRST: 170,
            }
        )
        spec = SPEC.model_copy(update={"preferences": prefs})
        return _solve([j, y], spec=spec)[0]

    brief = run(130.0)
    lines = [ln for ln in brief.sensitivity if "per flown hour" in ln]
    assert lines, brief.sensitivity
    be = float(re.search(r"\$([\d,]+) per flown hour", lines[0]).group(1).replace(",", ""))
    rec_cab = brief.recommendation.cabins
    above, below = run(be + 2), run(be - 2)
    cabins = {above.recommendation.cabins, below.recommendation.cabins}
    assert len(cabins) == 2 and rec_cab in cabins  # crossing the break-even flips the pick
