"""Property tests: invariants every allocator solution must satisfy."""

from __future__ import annotations

from collections import defaultdict
from datetime import UTC, date, datetime, timedelta, timezone

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from jetbridge.kb import load_kb
from jetbridge.models.core import (
    AwardOffer,
    Cabin,
    DateWindow,
    Itinerary,
    Money,
    Provenance,
    SeatInfo,
    Segment,
)
from jetbridge.models.spec import DestinationPref, Profile, SearchSpec
from jetbridge.solve.allocator import Allocator, max_pax_for
from jetbridge.solve.eligibility import Candidate

KB = load_kb()
TZ = timezone(timedelta(hours=-5))
PROGRAMS = ["american", "delta", "united", "aeroplan"]
INDEPENDENT = {"delta": True}


@st.composite
def offer(draw, idx: int):
    prog = draw(st.sampled_from(PROGRAMS))
    adapter = prog
    day = draw(st.integers(0, 2))
    hour = draw(st.integers(6, 20))
    fn = str(100 + idx)
    dep = datetime(2027, 5, 28 + day, hour, 0, tzinfo=TZ)
    cabin = draw(st.sampled_from([Cabin.ECONOMY, Cabin.BUSINESS]))
    seg = Segment(
        marketing_carrier="UA" if prog == "united" else "AA",
        flight_number=fn,
        origin="ORD",
        destination="HND",
        departs_at=dep,
        arrives_at=dep + timedelta(hours=13),
        cabin=cabin,
    )
    pax = draw(st.sampled_from([1, 3]))
    seats = draw(st.integers(pax, 9))
    return AwardOffer(
        program=prog,
        itinerary=Itinerary(segments=(seg,)),
        miles=draw(st.integers(30, 300)) * 1000,
        taxes=Money.usd(draw(st.integers(5, 400))),
        seats=SeatInfo(kind="exact", count=seats),
        provenance=Provenance(
            adapter=adapter,
            tier="replay",
            fetch_id="f",
            observed_at=datetime.now(UTC),
            request_pax=pax,
        ),
    )


@st.composite
def scenario(draw):
    n = draw(st.integers(4, 10))
    offers = [draw(offer(i)) for i in range(n)]
    balances = {}
    if draw(st.booleans()):
        for p in PROGRAMS:
            balances[p] = draw(st.integers(0, 900)) * 1000
        balances["chase_ur"] = draw(st.integers(0, 600)) * 1000
        balances["bilt"] = draw(st.integers(0, 600)) * 1000
    together = draw(st.sampled_from(["same_flights", "same_day", "any"]))
    return offers, balances, together


def _spec(together: str) -> SearchSpec:
    return SearchSpec(
        origins=["ORD"],
        destinations=[DestinationPref(code="HND")],
        outbound=DateWindow(start=date(2027, 5, 27), end=date(2027, 6, 2)),
        passengers=3,
        together=together,
    )


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(scenario())
def test_allocator_invariants(sc):
    offers, balances, together = sc
    spec = _spec(together)
    profile = Profile(balances=balances)
    cands = [Candidate(o, "out", 1.0) for o in offers]
    sols = Allocator(spec, profile, KB, INDEPENDENT).solve(cands, k=4, time_limit_s=5)
    keys = set()
    for s in sols:
        # covers exactly N passengers
        assert sum(a.pax for a in s.assignments) == spec.passengers
        # never exceeds the sound passenger range for an offer
        for a in s.assignments:
            lo, hi = max_pax_for(a.candidate, spec.passengers, INDEPENDENT)
            assert lo <= a.pax <= hi
            assert a.pax <= max(
                a.candidate.offer.seats.count, a.candidate.offer.provenance.request_pax
            )
        # balances respected (own + transfers)
        if balances:
            need = s.miles_by_program
            got: dict[str, int] = defaultdict(int)
            for t in s.transfers:
                got[t.program] += t.miles
            for p, m in need.items():
                assert balances.get(p, 0) + got[p] >= m, (p, m)
            sent: dict[str, int] = defaultdict(int)
            for t in s.transfers:
                sent[t.source] += t.points
            for src, pts in sent.items():
                assert pts <= balances[src]
        # together policy
        if together == "same_flights":
            assert len({a.candidate.group for a in s.assignments}) == 1
        if together == "same_day":
            assert len({a.candidate.offer.itinerary.departs_at.date() for a in s.assignments}) == 1
        # k-best solutions are distinct
        assert s.key() not in keys
        keys.add(s.key())
    # objective order is non-decreasing
    objs = [s.objective_cents for s in sols]
    # non-decreasing up to 1 cent (objective is floor-divided from cents*100)
    assert all(b >= a - 1 for a, b in zip(objs, objs[1:], strict=False)), objs


def test_dynamic_price_offer_cannot_be_split():
    """An AA offer seen in a 1-pax search can only carry exactly 1 passenger."""
    o = AwardOffer(
        program="american",
        itinerary=Itinerary(
            segments=(
                Segment(
                    marketing_carrier="AA",
                    flight_number="175",
                    origin="DFW",
                    destination="HND",
                    departs_at=datetime(2027, 5, 28, 10, 40, tzinfo=TZ),
                    arrives_at=datetime(2027, 5, 29, 14, 20, tzinfo=timezone(timedelta(hours=9))),
                    cabin=Cabin.BUSINESS,
                ),
            )
        ),
        miles=60000,
        taxes=Money.usd(5.6),
        seats=SeatInfo(kind="exact", count=9),
        provenance=Provenance(
            adapter="american",
            tier="replay",
            fetch_id="f",
            observed_at=datetime.now(UTC),
            request_pax=1,
        ),
    )
    assert max_pax_for(Candidate(o, "out", 1.0), 3, INDEPENDENT) == (1, 1)


def test_transfer_funding_used_when_program_balance_short():
    spec = _spec("any")
    dep = datetime(2027, 5, 28, 10, 0, tzinfo=TZ)
    o = AwardOffer(
        program="united",
        itinerary=Itinerary(
            segments=(
                Segment(
                    marketing_carrier="UA",
                    flight_number="881",
                    origin="ORD",
                    destination="HND",
                    departs_at=dep,
                    arrives_at=dep + timedelta(hours=13),
                    cabin=Cabin.BUSINESS,
                ),
            )
        ),
        miles=100_000,
        taxes=Money.usd(5.6),
        seats=SeatInfo(kind="exact", count=5),
        provenance=Provenance(
            adapter="united",
            tier="replay",
            fetch_id="f",
            observed_at=datetime.now(UTC),
            request_pax=3,
        ),
    )
    profile = Profile(balances={"united": 120_000, "chase_ur": 250_000})
    sols = Allocator(spec, profile, KB, INDEPENDENT).solve([Candidate(o, "out", 1.0)], k=1)
    assert sols, "expected a feasible solution via Chase UR transfer"
    s = sols[0]
    assert s.miles_by_program == {"united": 300_000}
    t = sum(t.miles for t in s.transfers if t.source == "chase_ur")
    assert t >= 180_000 and s.own_used.get("united", 0) <= 120_000
    # infeasible when transfers are not enough
    poor = Profile(balances={"united": 120_000, "chase_ur": 50_000})
    assert Allocator(spec, poor, KB, INDEPENDENT).solve([Candidate(o, "out", 1.0)], k=1) == []


def test_same_fare_seen_at_two_party_sizes_is_not_double_counted():
    """Regression (2026-10-31): a fare seen in a 3-pax and a 1-pax Delta search shares
    offer_id; it must remain two solver variables with their own passenger counts."""
    dep = datetime(2027, 5, 30, 9, 0, tzinfo=TZ)
    seg = Segment(
        marketing_carrier="AA",
        flight_number="197",
        origin="ORD",
        destination="HND",
        departs_at=dep,
        arrives_at=dep + timedelta(hours=13),
        cabin=Cabin.ECONOMY,
    )

    def mk(pax: int) -> AwardOffer:
        return AwardOffer(
            program="american",
            itinerary=Itinerary(segments=(seg,)),
            miles=98_400,
            taxes=Money.usd(5.6),
            seats=SeatInfo(kind="lower_bound", count=9),
            provenance=Provenance(
                adapter="american",
                tier="replay",
                fetch_id="f",
                observed_at=datetime.now(UTC),
                request_pax=pax,
            ),
        )

    a3, a1 = mk(3), mk(1)
    assert a3.offer_id == a1.offer_id  # same fare identity...
    c3, c1 = Candidate(a3, "out", 1.0), Candidate(a1, "out", 1.0)
    assert c3.id != c1.id  # ...but distinct decision variables
    sols = Allocator(_spec("same_flights"), Profile(), KB, INDEPENDENT).solve([c3, c1], k=3)
    assert sols
    for s in sols:
        assert sum(a.pax for a in s.assignments) == 3
        for a in s.assignments:
            assert a.pax == a.candidate.offer.provenance.request_pax
