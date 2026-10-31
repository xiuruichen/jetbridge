"""Parser tests against real responses recorded during Phase 0 recon (2026-10-31)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from jetbridge.acquire.adapters.american import AmericanAdapter
from jetbridge.acquire.adapters.delta import DeltaAdapter
from jetbridge.acquire.adapters.united import UnitedAdapter
from jetbridge.acquire.types import Query, RawResponse
from jetbridge.models.core import Cabin, Provenance

FIX = Path(__file__).resolve().parents[1] / "fixtures"


def _raw(body: str) -> RawResponse:
    return RawResponse(
        status=200, body=body, tier="replay", started_at=datetime.now(UTC), latency_ms=0
    )


def _prov(adapter: str) -> Provenance:
    return Provenance(
        adapter=adapter,
        tier="replay",
        fetch_id="f_test",
        observed_at=datetime.now(UTC),
        request_pax=1,
    )


def _q(adapter: str, o: str, d: str) -> Query:
    return Query(adapter=adapter, kind="day", origin=o, destination=d, date=date(2027, 5, 28))


# ---------------------------------------------------------------- United
@pytest.fixture(scope="module")
def united():
    body = (FIX / "united" / "ord_hnd_award_20270528.sse").read_text(encoding="utf-8")
    a = UnitedAdapter()
    return a.parse(_raw(body), _q("united", "ORD", "HND"), _prov("united"))


def test_united_parses_all_options(united):
    assert not united.warnings, united.warnings
    flights = {o.itinerary.flight_key for o in united.awards}
    assert len(flights) == 30  # 30 flightOption events in the capture


def test_united_nonstop_business_price_and_seats(united):
    ua867 = [
        o
        for o in united.awards
        if o.itinerary.flight_key.startswith("UA867@ORD")
        and o.itinerary.main_cabin == Cabin.BUSINESS
    ]
    assert ua867, "UA867 business offer missing"
    o = ua867[0]
    assert o.miles == 250_000
    assert str(o.taxes.amount) == "5.60"
    # bookingClassAvailability had JN9 -> at least 9 seats
    assert o.seats.kind == "lower_bound" and o.seats.count == 9
    seg = o.itinerary.segments[0]
    assert seg.departs_at.isoformat() == "2027-05-28T11:20:00-05:00"
    assert seg.arrives_at.isoformat() == "2027-05-29T14:25:00+09:00"


def test_united_connection_cabins_are_per_segment(united):
    # ORD-LAX (domestic first) + LAX-NRT (Polaris) priced as business
    o = next(
        o
        for o in united.awards
        if o.itinerary.flight_key.startswith("UA1333@ORD")
        and "UA32@LAX" in o.itinerary.flight_key
        and o.itinerary.main_cabin == Cabin.BUSINESS
    )
    cabins = [s.cabin for s in o.itinerary.segments]
    assert cabins == [Cabin.FIRST, Cabin.BUSINESS]
    assert o.itinerary.segments[1].origin == "LAX"


def test_united_partner_segment(united):
    ac = [
        o for o in united.awards if any(s.marketing_carrier == "AC" for s in o.itinerary.segments)
    ]
    assert ac, "Air Canada connection missing"
    assert all(o.itinerary.segments[-1].destination == "HND" for o in ac)


# ---------------------------------------------------------------- American
@pytest.fixture(scope="module")
def american():
    state = (FIX / "american" / "dfw_hnd_award_20270528.ngstate.json").read_text(encoding="utf-8")
    html = f'<html><script id="ng-state" type="application/json">{state}</script></html>'
    a = AmericanAdapter()
    return a.parse(_raw(html), _q("american", "DFW", "HND"), _prov("american"))


def test_american_parses(american):
    assert not american.warnings, american.warnings
    flights = {o.itinerary.flight_key for o in american.awards}
    assert len(flights) == 40


def test_american_nonstop(american):
    aa175 = {
        o.itinerary.main_cabin: o
        for o in american.awards
        if o.itinerary.flight_key.startswith("AA175@DFW")
    }
    assert aa175[Cabin.ECONOMY].miles == 143_000
    assert aa175[Cabin.PREMIUM].miles == 200_000
    assert aa175[Cabin.BUSINESS].miles == 363_500
    assert str(aa175[Cabin.BUSINESS].taxes.amount) == "5.60"
    # seatsRemaining == 0 means unknown, never "sold out"
    assert aa175[Cabin.BUSINESS].seats.kind == "lower_bound"
    seg = aa175[Cabin.BUSINESS].itinerary.segments[0]
    assert seg.departs_at.isoformat() == "2027-05-28T10:40:00-05:00"


def test_american_airport_change_detected(american):
    # DFW-LGA + JFK-HND itineraries exist in the capture
    changes = [o for o in american.awards if o.itinerary.airport_changes]
    pairs = {c for o in changes for c in o.itinerary.airport_changes}
    assert {("LGA", "JFK"), ("EWR", "JFK")} <= pairs


# ---------------------------------------------------------------- Delta
@pytest.fixture(scope="module")
def delta():
    body = (FIX / "delta" / "dtw_icn_award_20270528.json").read_text(encoding="utf-8")
    a = DeltaAdapter()
    return a.parse(_raw(body), _q("delta", "DTW", "ICN"), _prov("delta"))


def test_delta_parses(delta):
    assert not delta.warnings, delta.warnings
    assert len({o.itinerary.flight_key for o in delta.awards}) == 11


def test_delta_nonstop_delta_one_exact_seats(delta):
    d1 = next(
        o
        for o in delta.awards
        if o.itinerary.flight_key.startswith("DL159@DTW")
        and o.itinerary.main_cabin == Cabin.BUSINESS
    )
    assert d1.miles == 499_900
    assert d1.seats.kind == "exact" and d1.seats.count == 4
    assert str(d1.taxes.amount) == "5.60"
    seg = d1.itinerary.segments[0]
    assert seg.departs_at.isoformat() == "2027-05-28T12:45:00-04:00"  # EDT
    assert seg.arrives_at.isoformat() == "2027-05-29T16:25:00+09:00"


def test_delta_mixed_cabin(delta):
    # DTW-SLC in Comfort + SLC-ICN in Premium Select, priced as Premium Select
    o = next(
        o
        for o in delta.awards
        if "DL173@SLC" in o.itinerary.flight_key and o.itinerary.main_cabin == Cabin.PREMIUM
    )
    assert [s.cabin for s in o.itinerary.segments] == [Cabin.ECONOMY, Cabin.PREMIUM]
    assert 0 < o.itinerary.cabin_mix(Cabin.PREMIUM) < 1


def test_delta_sold_out_brands_skipped(delta):
    # DL3168/DL27 had Basic sold out; it must not appear as an offer
    basic = [
        o
        for o in delta.awards
        if o.itinerary.flight_key.startswith("DL3168@DTW") and o.miles == 129_900
    ]
    assert basic == []


def test_json_roundtrip_of_offer(delta):
    o = delta.awards[0]
    again = type(o).model_validate_json(o.model_dump_json())
    assert again == o and again.offer_id == o.offer_id
    json.loads(o.model_dump_json())


def test_aa_challenge_page_is_blocked_not_empty():
    from jetbridge.acquire.detect import classify
    from jetbridge.acquire.types import Outcome

    page = "<html><head><title>Challenge Validation</title></head><body>Please confirm you're a human</body></html>"
    out, _ = classify(_raw(page), require_marker='id="ng-state"')
    assert out is Outcome.BLOCKED


def test_aa_error_code_is_not_a_genuine_empty():
    a = AmericanAdapter()
    state = json.dumps(
        {"SearchData": {"itineraryResult": {"error": "309", "slices": [], "responseMetadata": {}}}}
    )
    assert not a.is_empty_answer(_raw(f'<script id="ng-state">{state}</script>'))
    clean = json.dumps(
        {"SearchData": {"itineraryResult": {"error": "", "slices": [], "responseMetadata": {}}}}
    )
    assert a.is_empty_answer(_raw(f'<script id="ng-state">{clean}</script>'))


# ---------------------------------------------------------------- Alaska
def test_alaska_parses_exact_seats_and_hawaiian_metal():
    from jetbridge.acquire.adapters.alaska import AlaskaAdapter

    body = (FIX / "alaska" / "sea_hnd_award_20270528.json").read_text(encoding="utf-8")
    res = AlaskaAdapter().parse(_raw(body), _q("alaska", "SEA", "HND"), _prov("alaska"))
    assert not res.warnings, res.warnings
    assert len({o.itinerary.flight_key for o in res.awards}) == 12
    o = next(
        o
        for o in res.awards
        if o.itinerary.flight_key.startswith("AS843@SEA")
        and o.itinerary.main_cabin == Cabin.BUSINESS
    )
    assert o.miles == 200_000 and o.seats.kind == "exact" and o.seats.count == 3
    assert [s.cabin for s in o.itinerary.segments] == [Cabin.FIRST, Cabin.BUSINESS]
    assert o.itinerary.segments[-1].operator == "HA"
    assert o.itinerary.segments[-1].arrives_at.isoformat().endswith("+09:00")


# ---------------------------------------------------------------- Google Flights
def test_google_flights_total_price_divided_per_pax():
    from jetbridge.acquire.adapters.google_flights import GoogleFlightsAdapter

    body = (FIX / "google_flights" / "cvg_icn_J_3pax_20270528.json").read_text(encoding="utf-8")
    q = Query("google_flights", "day", "CVG", "ICN", date(2027, 5, 28), 3, Cabin.BUSINESS)
    prov = Provenance(
        adapter="google_flights",
        tier="replay",
        fetch_id="f",
        observed_at=datetime.now(UTC),
        request_pax=3,
    )
    res = GoogleFlightsAdapter().parse(_raw(body), q, prov)
    assert not res.warnings, res.warnings
    assert len(res.cash) >= 8
    ac = next(c for c in res.cash if c.itinerary.flight_key.startswith("AC8704@CVG"))
    assert str(ac.price.amount) == "4826.67"  # $14,480 total / 3
    seg = ac.itinerary.segments[0]
    assert seg.departs_at.isoformat() == "2027-05-28T09:15:00-04:00"
    assert ac.itinerary.segments[-1].destination == "ICN"
    assert all(s.cabin == Cabin.BUSINESS for s in ac.itinerary.segments)
