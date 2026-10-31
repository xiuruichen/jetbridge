"""Offline end-to-end: recorded responses -> engine interpret -> solve -> report."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

FIX = Path(__file__).resolve().parents[1] / "fixtures"


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_VAR", str(tmp_path))
    from jetbridge.store import db

    db._engines.clear()
    yield


def _raw(body: str):
    from jetbridge.acquire.types import RawResponse

    return RawResponse(
        status=200, body=body, tier="replay", started_at=datetime.now(UTC), latency_ms=0
    )


def _collected():
    from jetbridge.acquire.adapters import registry
    from jetbridge.acquire.cache import record_fetch
    from jetbridge.acquire.engine import Engine
    from jetbridge.acquire.types import Outcome, Query
    from jetbridge.runner import Collected

    ad = registry()
    eng = Engine(ad, broker=None)
    state = (FIX / "american" / "dfw_hnd_award_20270528.ngstate.json").read_text(encoding="utf-8")
    bodies = {
        Query("united", "day", "ORD", "HND", date(2027, 5, 28), 1): (
            FIX / "united" / "ord_hnd_award_20270528.sse"
        ).read_text(encoding="utf-8"),
        Query("american", "day", "DFW", "HND", date(2027, 5, 28), 1): (
            f'<script id="ng-state" type="application/json">{state}</script>'
        ),
        Query("delta", "day", "DTW", "ICN", date(2027, 5, 28), 1): "["
        + (FIX / "delta" / "dtw_icn_award_20270528.json").read_text(encoding="utf-8")
        + "]",
    }
    col = Collected()
    for q, body in bodies.items():
        raw = _raw(body)
        fid = record_fetch(q, raw, Outcome.OK)
        res = eng._interpret(ad[q.adapter], q, raw, fid)
        assert res.status == "ok", (q, res.reason)
        col.results.append(("out", res))
    return col


def _spec(**kw):
    from jetbridge.models.spec import SearchSpec

    base = {
        "origins": ["ORD", "DFW", "DTW"],
        "destinations": [{"code": "TYO", "weight": 1.0}, {"code": "ICN", "weight": 0.9}],
        "outbound": {"start": "2027-05-27", "end": "2027-05-29"},
        "passengers": 1,
        "preferred_cabin": "J",
    }
    base.update(kw)
    return SearchSpec.model_validate(base)


def test_offline_pipeline_produces_ranked_verified_report():
    from jetbridge.acquire.adapters import registry
    from jetbridge.kb import load_kb
    from jetbridge.models.core import Cabin
    from jetbridge.models.spec import Profile
    from jetbridge.report.render import write_report
    from jetbridge.runner import _store_options, create_search, solve

    spec = _spec()
    profile = Profile()
    sid, _plan = create_search(spec, profile)
    sols, cands, stats = solve(spec, profile, load_kb(), _collected(), registry())
    assert sols, "expected at least one option"
    assert stats.kept > 50
    best = sols[0]
    assert best.label == "best overall"
    # preferred cabin is business: the best overall option must be business class
    assert all(a.candidate.offer.itinerary.main_cabin >= Cabin.BUSINESS for a in best.assignments)
    # the cheapest business seen in fixtures is UA 250k vs AA 353k/358k vs DL 499.9k
    assert best.miles_by_program == {"united": 250_000}
    # diverse alternatives: at least one per program
    progs = {p for s in sols for p in s.miles_by_program}
    assert {"united", "american", "delta"} <= progs

    ids = _store_options(sid, sols, {})
    path = write_report(sid, spec, profile, load_kb(), sols, {}, ids, cands, stats)
    md = path.read_text(encoding="utf-8")
    assert "250,000" in md and "Coverage ledger" in md and ids[0] in md
    html = (path.parent / "report.html").read_text(encoding="utf-8")
    assert "<table>" in html
    summary = json.loads((path.parent / "summary.json").read_text(encoding="utf-8"))
    assert summary["options"][0]["id"] == ids[0]


def test_balances_change_the_answer():
    """With only Delta miles, the solver must pick Delta even though United is cheaper."""
    from jetbridge.acquire.adapters import registry
    from jetbridge.kb import load_kb
    from jetbridge.models.spec import Profile
    from jetbridge.runner import solve

    spec = _spec()
    sols, _, _ = solve(
        spec, Profile(balances={"delta": 600_000}), load_kb(), _collected(), registry()
    )
    assert sols and set(sols[0].miles_by_program) == {"delta"}
    assert all(set(s.miles_by_program) <= {"delta"} for s in sols)


def test_min_cabin_is_hard():
    from jetbridge.acquire.adapters import registry
    from jetbridge.kb import load_kb
    from jetbridge.models.core import Cabin
    from jetbridge.models.spec import Profile
    from jetbridge.runner import solve

    spec = _spec(min_cabin="J")
    sols, cands, stats = solve(spec, Profile(), load_kb(), _collected(), registry())
    assert all(c.offer.itinerary.main_cabin >= Cabin.BUSINESS for c in cands)
    assert stats.rejected.get("main cabin below minimum BUSINESS", 0) > 0


def test_gateway_award_composed_with_positioning_end_to_end():
    """Recorded Delta DTW->ICN award space + a CVG->DTW cash positioning leg must yield a
    composed CVG->DTW->ICN candidate on separate tickets, and the advisor must explain it."""
    from datetime import timedelta, timezone

    from jetbridge.acquire.adapters import registry
    from jetbridge.acquire.engine import QueryResult
    from jetbridge.acquire.types import ParseResult
    from jetbridge.kb import load_kb
    from jetbridge.models.core import Cabin, CashOffer, Itinerary, Money, Provenance, Segment
    from jetbridge.models.spec import Profile, SearchSpec
    from jetbridge.runner import build_candidates, optimize
    from jetbridge.solve.advisor import advise

    col = _collected()  # includes Delta DTW->ICN (2027-05-28) from the fixture
    et = timezone(timedelta(hours=-4))
    dep = datetime(2027, 5, 28, 7, 0, tzinfo=et)
    pos = CashOffer(
        source="google_flights",
        itinerary=Itinerary(
            segments=(
                Segment(
                    marketing_carrier="DL",
                    flight_number="5307",
                    origin="CVG",
                    destination="DTW",
                    departs_at=dep,
                    arrives_at=dep + timedelta(minutes=75),
                    cabin=Cabin.ECONOMY,
                ),
            )
        ),
        price=Money.usd(139),
        provenance=Provenance(
            adapter="google_flights",
            tier="replay",
            fetch_id="f",
            observed_at=datetime.now(UTC),
            request_pax=1,
        ),
    )
    col.results.append(
        ("out", QueryResult(query=None, status="ok", parsed=ParseResult(cash=[pos])))
    )  # type: ignore[arg-type]
    spec = SearchSpec.model_validate(
        {
            "origins": ["CVG"],
            "destinations": [{"code": "ICN"}],
            "outbound": {"start": "2027-05-28", "end": "2027-05-28"},
            "passengers": 1,
            "preferred_cabin": "J",
        }
    )
    kb = load_kb()
    cands, stats, gw, positioning = build_candidates(spec, kb, col)
    assert gw and positioning
    composed = [c for c in cands if c.composed]
    assert composed, stats.rejected
    c = composed[0]
    assert c.itinerary.origin == "CVG" and c.itinerary.destination == "ICN"
    assert c.positioning[0].offer_id == pos.offer_id
    lv = c.quality.layovers[0]
    assert lv.separate_ticket and lv.minutes >= 180  # buffer respected
    res = optimize(spec, Profile(), kb, col, registry())
    assert res.solutions, "composed candidates should make the CVG trip feasible"
    ids = [f"t{i}" for i in range(len(res.solutions))]
    brief = advise(res.solutions, ids, {}, spec, Profile(), kb, res.cash_index)
    assert brief.recommendation is not None
    rec_comp = any(a.candidate.composed for a in brief.recommendation.solution.assignments)
    if rec_comp:
        assert any("positioning" in a for a in brief.actions)
    assert any("Positioning on a separate ticket" in i for i in brief.insights)
