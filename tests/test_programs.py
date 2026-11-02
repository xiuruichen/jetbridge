"""Delta and United alongside American: real recordings, failures, limits and independence."""

from __future__ import annotations

import json
from copy import deepcopy
from datetime import date
from pathlib import Path

import pytest
from conftest import (
    DAY,
    add_cx,
    answer_from,
    block_page,
    cx_slice,
    make_runner,
    response,
    to_html,
)

from jetbridge.acquire import american, delta, united
from jetbridge.acquire.detect import classify
from jetbridge.acquire.fetcher import LiveFetcher
from jetbridge.acquire.governor import BUDGETS, Governor
from jetbridge.acquire.sites import PROGRAMS, SITES, get_site, parse_program
from jetbridge.acquire.types import Outcome, RawResponse, SchemaDrift
from jetbridge.matcher import evaluate
from jetbridge.models import AnswerKind, Cabin, CheckStatus, Flight, Query, SearchAnswer, Watch
from jetbridge.service import create_watch

FIXTURES = Path(__file__).parent / "fixtures"
UNITED_BODY = (FIXTURES / "united" / "ord_hnd_award_20270528.sse").read_text(encoding="utf-8")
DELTA_BODY = (FIXTURES / "delta" / "dtw_icn_award_20270528.json").read_text(encoding="utf-8")
RECORDED_DAY = date(2027, 5, 28)
Q = Query("JFK", "HKG", DAY)


def watch(code, origin, destination, cabin, program, **kw) -> Watch:
    return Watch(
        id=1,
        flight=Flight.parse(code),
        origin=origin,
        destination=destination,
        dates=(RECORDED_DAY,),
        cabin=cabin,
        program=program,
        **kw,
    )


def check(parsed, w):
    return evaluate(w, RECORDED_DAY, SearchAnswer(AnswerKind.ANSWER, parsed=parsed))


def ua(code="UA867", cabin=Cabin.BUSINESS, body=UNITED_BODY, destination="NRT", **kw):
    return check(united.parse(body), watch(code, "ORD", destination, cabin, "united", **kw))


def dl(code="DL159", cabin=Cabin.BUSINESS, body=DELTA_BODY, **kw):
    return check(delta.parse(body), watch(code, "DTW", "ICN", cabin, "delta", **kw))


# ---- United, recorded ---------------------------------------------------------------------
def test_united_recording_parses_completely():
    parsed = united.parse(UNITED_BODY)
    assert len(parsed.slices) == 30 and not parsed.unreadable and parsed.site_error is None


def test_united_nonstop_business_price_and_capped_seats():
    result = ua()
    assert (result.status, result.miles) == (CheckStatus.AVAILABLE, 250_000)
    assert str(result.taxes) == "5.60"
    assert result.seats_exact is None and result.seats_text == "at least 9"


def test_united_city_wide_results_only_match_the_watched_airport():
    # A search for HND also returns Narita flights, because United searches all of Tokyo.
    assert ua("UA867", destination="HND").status is CheckStatus.NOT_LISTED
    assert ua("UA881", destination="HND").status is CheckStatus.AVAILABLE


def test_united_flight_only_seen_on_a_connection_is_not_this_itinerary():
    assert ua("UA1333").status is CheckStatus.NOT_LISTED


def test_united_seat_is_judged_by_what_is_flown_not_the_product_name():
    # A "business" product flown in First counts as first on a single flight.
    flight = {
        "marketingCarrier": "UA",
        "flightNumber": "1",
        "origin": "ORD",
        "destination": "LAX",
        "departDateTime": "2027-05-28 08:00",
        "destinationDateTime": "2027-05-28 10:00",
        "bookingClassAvailability": "IN3",
        "products": [
            {
                "productType": "MIN-BUSINESS-SURP-OR-DISP",
                "cabinType": "First",
                "bookingCode": "IN",
                "prices": [
                    {"currency": "MILES", "amount": 25000},
                    {"pricingType": "Tax", "amount": 5.6, "currency": "USD"},
                ],
            }
        ],
    }
    parsed = united.parse(_stream(flight))
    w = watch("UA1", "ORD", "LAX", Cabin.FIRST, "united")
    assert check(parsed, w).status is CheckStatus.AVAILABLE
    assert check(parsed, w).seats_exact == 3
    business = watch("UA1", "ORD", "LAX", Cabin.BUSINESS, "united")
    assert check(parsed, business).status is CheckStatus.NOT_OFFERED


def _stream(*flights, status="success", end=True) -> str:
    events = [{"type": "meta", "status": status}]
    events += [{"type": "flightOption", "flight": f} for f in flights]
    if end:
        events.append({"type": "streamingTimings"})
    return "\n".join(f"data: {json.dumps(e)}" for e in events)


def test_united_genuine_empty_is_not_listed():
    assert ua(body=_stream()).status is CheckStatus.NOT_LISTED


@pytest.mark.parametrize(
    "body",
    [
        _stream(end=False),  # cut off, so the flight could be missing
        _stream(status="failed"),
        _stream() + "\ndata: {broken",
        _stream({"marketingCarrier": "UA", "flightNumber": "867"}),  # no products
    ],
)
def test_united_doubtful_streams_are_unknown(body):
    assert ua(body=body).status is CheckStatus.UNKNOWN


def test_united_unreadable_other_flight_does_not_hide_the_answer():
    body = _stream({"marketingCarrier": "UA", "flightNumber": "999"})
    assert ua("UA867", body=body).status is CheckStatus.NOT_LISTED


@pytest.mark.parametrize("body", ["", "<html>hello</html>", "data: {}"])
def test_united_without_meta_is_drift(body):
    with pytest.raises(SchemaDrift):
        united.parse(body)


# ---- Delta, recorded ----------------------------------------------------------------------
def test_delta_recording_parses_completely():
    parsed = delta.parse(DELTA_BODY)
    assert len(parsed.slices) == 11 and not parsed.unreadable and parsed.site_error is None


def test_delta_one_price_and_exact_seats():
    result = dl()
    assert (result.status, result.miles, result.seats_exact) == (
        CheckStatus.AVAILABLE,
        499_900,
        4,
    )
    assert str(result.taxes) == "5.60"


def test_delta_cheapest_fare_in_the_cabin_and_capped_seats():
    result = dl(cabin=Cabin.ECONOMY)
    assert result.miles == 129_900 and result.seats_text == "at least 9"


def test_delta_connection_flight_is_not_this_itinerary():
    assert dl("DL173").status is CheckStatus.NOT_LISTED


def _delta_data() -> dict:
    return json.loads(DELTA_BODY)


def _only_dl159(data: dict) -> dict:
    sets = data["data"]["gqlSearchOffers"]["gqlOffersSets"]
    data["data"]["gqlSearchOffers"]["gqlOffersSets"] = [
        s
        for s in sets
        if [
            f"{seg['marketingCarrier']['carrierCode']}{seg['marketingCarrier']['carrierNum']}"
            for t in s["trips"]
            for seg in t["flightSegment"]
        ]
        == ["DL159"]
    ]
    return data


def test_delta_sold_out_is_not_offered():
    data = _only_dl159(_delta_data())
    for offer in data["data"]["gqlSearchOffers"]["gqlOffersSets"][0]["offers"]:
        offer["soldOut"] = True
    assert dl(body=json.dumps(data)).status is CheckStatus.NOT_OFFERED


# A real ATL-CDG response from 2026-11-01, trimmed to three flights. It first exposed AFPE.
ATL_CDG = (FIXTURES / "delta" / "atl_cdg_award_20270521.json").read_text(encoding="utf-8")


def atl_cdg(code, cabin, body=ATL_CDG):
    w = Watch(
        id=1,
        flight=Flight.parse(code),
        origin="ATL",
        destination="CDG",
        dates=(DAY,),
        cabin=cabin,
        program="delta",
    )
    return evaluate(w, DAY, SearchAnswer(AnswerKind.ANSWER, parsed=delta.parse(body)))


def test_air_france_premium_fare_is_read_as_premium():
    result = atl_cdg("AF33", Cabin.PREMIUM)
    assert (result.status, result.miles, result.seats_exact) == (
        CheckStatus.AVAILABLE,
        411_700,
        7,
    )


def test_air_france_premium_fare_is_never_business():
    assert atl_cdg("AF33", Cabin.BUSINESS).status is CheckStatus.NOT_OFFERED


def test_recorded_atl_cdg_response_reads_completely():
    parsed = delta.parse(ATL_CDG)
    assert not parsed.unreadable and not any(s.unknown_fares for s in parsed.slices)


# ---- partner fares: Delta's cabin columns ------------------------------------------------
# A real CDG-NBO response from 2026-11-01. Every flight is a partner's, so it has no Delta brands.
CDG_NBO = (FIXTURES / "delta" / "cdg_nbo_award_20270521.json").read_text(encoding="utf-8")
PARTNER_DAY = DAY


def fare(brands, column, *, dominant=None, label=None, miles=100_000, seats=4, sold_out=False):
    """One column of a Delta itinerary, in the shape of a recorded response."""
    brands = [brands] if isinstance(brands, str) else brands
    return {
        "soldOut": sold_out,
        "additionalOfferProperties": {
            "offerPriorityNum": column,
            "dominantSegmentBrandId": dominant or brands[0],
        },
        "offerItems": [
            {
                "retailItems": [
                    {
                        "retailItemMetaData": {
                            "fareInformation": [
                                {
                                    "solutionId": f"x/USD/{label}" if label else "x",
                                    "availableSeatCnt": seats,
                                    "brandByFlightLegs": [{"brandId": b} for b in brands],
                                    "farePrice": [
                                        {
                                            "totalFarePrice": {
                                                "milesEquivalentPrice": {"mileCnt": miles},
                                                "currencyEquivalentPrice": {
                                                    "formattedCurrencyAmt": "5.60"
                                                },
                                            }
                                        }
                                    ],
                                }
                            ]
                        }
                    }
                ]
            }
        ],
    }


def itinerary(code, *offers, legs=1):
    carrier, number = code[:2], code[2:]
    stops = ["AAA", *[f"X{i:02d}" for i in range(legs - 1)], "BBB"]
    return {
        "trips": [
            {
                "flightSegment": [
                    {
                        "marketingCarrier": {"carrierCode": carrier, "carrierNum": number},
                        "flightLeg": [
                            {
                                "originAirportCode": stops[i],
                                "destinationAirportCode": stops[i + 1],
                                "scheduledDepartureLocalTs": f"{DAY}T10:00",
                                "scheduledArrivalLocalTs": f"{DAY}T20:00",
                            }
                            for i in range(legs)
                        ],
                    }
                ]
            }
        ],
        "offers": list(offers),
    }


def reply(*itineraries) -> str:
    return json.dumps({"data": {"gqlSearchOffers": {"gqlOffersSets": list(itineraries)}}})


def seat(body, cabin, code="XX1"):
    w = Watch(
        id=1,
        flight=Flight.parse(code),
        origin="AAA",
        destination="BBB",
        dates=(DAY,),
        cabin=cabin,
        program="delta",
    )
    return evaluate(w, DAY, SearchAnswer(AnswerKind.ANSWER, parsed=delta.parse(body)))


OWN = itinerary("DL1", fare("CD1", 8), fare("CDPS", 5), fare("CMAIN", 2))


@pytest.mark.parametrize(
    ("column", "cabin"), [(2, Cabin.ECONOMY), (5, Cabin.PREMIUM), (8, Cabin.BUSINESS)]
)
def test_new_partner_brand_is_read_from_its_column(column, cabin):
    body = reply(OWN, itinerary("XX1", fare("NEWPARTNER", column)))
    assert seat(body, cabin).status is CheckStatus.AVAILABLE


def test_new_partner_brand_is_not_reported_in_any_other_cabin():
    body = reply(OWN, itinerary("XX1", fare("NEWPARTNER", 5)))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.NOT_OFFERED
    assert seat(body, Cabin.ECONOMY).status is CheckStatus.NOT_OFFERED


def test_sold_out_delta_columns_still_teach_the_rule():
    own = itinerary("DL1", fare("CD1", 77, sold_out=True))
    body = reply(own, itinerary("XX1", fare("NEWPARTNER", 77)))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.AVAILABLE


def test_this_response_beats_what_columns_usually_mean():
    own = itinerary("DL1", fare("CDPS", 8))  # Delta has renumbered its columns
    body = reply(own, itinerary("XX1", fare("NEWPARTNER", 8)))
    assert seat(body, Cabin.PREMIUM).status is CheckStatus.AVAILABLE
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.NOT_OFFERED


def test_column_never_seen_before_is_unknown_and_names_the_fare():
    result = seat(reply(OWN, itinerary("XX1", fare("NEWPARTNER", 99))), Cabin.BUSINESS)
    assert result.status is CheckStatus.UNKNOWN and result.suspect_parser
    assert "NEWPARTNER" in result.detail


def test_column_holding_two_cabins_is_unknown():
    own = itinerary("DL1", fare("CD1", 77), fare("CDPS", 77))
    body = reply(own, itinerary("XX1", fare("NEWPARTNER", 77)))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.UNKNOWN


def test_column_and_label_disagreeing_is_unknown():
    labelled = fare("NEWPARTNER", 8, label="NEWPARTNER:AWARD-PREMIUM-PLUS")
    body = reply(OWN, itinerary("XX1", labelled))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.UNKNOWN
    assert seat(body, Cabin.PREMIUM).status is CheckStatus.UNKNOWN


def test_a_brand_not_heading_its_column_learns_nothing_from_it():
    # A cheaper cabin can be on one leg of a business column, so this must not read as business.
    connection = fare(["NEWPARTNER", "CD1"], 8, dominant="CD1")
    body = reply(OWN, itinerary("XX1", connection, legs=2))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.UNKNOWN


def test_a_brand_learned_elsewhere_applies_on_a_lesser_leg():
    # AFST really appeared like this: economy in Main, and on a leg of a KLM business column.
    connection = fare(["NEWPARTNER", "CD1"], 8, dominant="CD1")
    body = reply(
        OWN,
        itinerary("XX2", fare("NEWPARTNER", 2)),
        itinerary("XX1", connection, legs=2),
    )
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.NOT_OFFERED


def test_partner_flight_can_use_a_label_alone():
    labelled = fare("NEWPARTNER", 99, label="NEWPARTNER:AWARD-PREMIUM-PLUS")
    assert seat(reply(itinerary("XX1", labelled)), Cabin.PREMIUM).status is CheckStatus.AVAILABLE


def test_unknown_fare_does_not_hide_readable_seats_on_the_same_flight():
    body = reply(OWN, itinerary("XX1", fare("CD1", 8, miles=90_000), fare("NEWPARTNER", 99)))
    assert seat(body, Cabin.BUSINESS).miles == 90_000
    assert seat(body, Cabin.ECONOMY).status is CheckStatus.UNKNOWN


def test_unknown_fare_on_another_flight_changes_nothing():
    body = reply(OWN, itinerary("XX2", fare("NEWPARTNER", 99)), itinerary("XX1", fare("CD1", 8)))
    assert seat(body, Cabin.BUSINESS).status is CheckStatus.AVAILABLE
    assert seat(body, Cabin.PREMIUM).status is CheckStatus.NOT_OFFERED


def test_real_all_partner_response_uses_the_usual_columns():
    decoded = json.loads(CDG_NBO)
    learned = delta.learn_brands(decoded if isinstance(decoded, list) else [decoded])
    assert learned["AFST"] is Cabin.ECONOMY and learned["KLBU"] is Cabin.BUSINESS


def test_real_partner_flight_with_only_economy_is_clear_in_every_cabin():
    w = Watch(
        id=1,
        flight=Flight.parse("AF828"),
        origin="CDG",
        destination="NBO",
        dates=(DAY,),
        cabin=Cabin.ECONOMY,
        program="delta",
    )
    answer = SearchAnswer(AnswerKind.ANSWER, parsed=delta.parse(CDG_NBO))
    assert evaluate(w, DAY, answer).status is CheckStatus.AVAILABLE
    business = Watch(**{**w.__dict__, "cabin": Cabin.BUSINESS})
    assert evaluate(business, DAY, answer).status is CheckStatus.NOT_OFFERED


# ---- partner fares: American and United read the seat, whatever the product is called -----
def _aa_answer(product="BUSINESS", cabin_type="BUSINESS", extra=None):
    item = cx_slice()
    item["pricingDetail"].append(
        {
            "productType": product,
            "productAvailable": True,
            "perPassengerAwardPoints": 88_000,
            "perPassengerTaxesAndFees": {"amount": 5.6, "currency": "USD"},
        }
    )
    item["segments"][0]["legs"][0]["productDetails"].append(
        {"productType": product, "cabinType": cabin_type}
    )
    return SearchAnswer(AnswerKind.ANSWER, parsed=american.parse(to_html(response(item))))


def _aa_watch(cabin):
    return Watch(
        id=1,
        flight=Flight.parse("CX805"),
        origin="JFK",
        destination="HKG",
        dates=(DAY,),
        cabin=cabin,
        program="american",
    )


def test_american_new_product_name_is_read_from_the_seat():
    result = evaluate(_aa_watch(Cabin.FIRST), DAY, _aa_answer("NEW_PRODUCT", "FIRST"))
    assert result.status is CheckStatus.AVAILABLE and result.miles == 88_000


def test_american_business_product_flown_in_first_counts_as_first():
    answer = _aa_answer("PARTNER_BUSINESS", "FIRST")
    assert evaluate(_aa_watch(Cabin.FIRST), DAY, answer).status is CheckStatus.AVAILABLE


def test_american_unknown_seat_does_not_hide_readable_seats_or_claim_none():
    answer = _aa_answer("NEW_PRODUCT", "SUITE")
    assert evaluate(_aa_watch(Cabin.BUSINESS), DAY, answer).status is CheckStatus.AVAILABLE
    first = evaluate(_aa_watch(Cabin.FIRST), DAY, answer)
    assert first.status is CheckStatus.UNKNOWN and "SUITE" in first.detail


def test_united_unknown_seat_does_not_hide_readable_seats_or_claim_none():
    def product(kind, cabin, code, miles):
        return {
            "productType": kind,
            "cabinType": cabin,
            "bookingCode": code,
            "prices": [
                {"currency": "MILES", "amount": miles},
                {"pricingType": "Tax", "amount": 5.6, "currency": "USD"},
            ],
        }

    flight = {
        "marketingCarrier": "UA",
        "flightNumber": "1",
        "origin": "ORD",
        "destination": "LAX",
        "departDateTime": "2027-05-28 08:00",
        "destinationDateTime": "2027-05-28 10:00",
        "bookingClassAvailability": "JN3",
        "products": [product("A", "Business", "JN", 50_000), product("B", "Suite", "Z", 90_000)],
    }
    parsed = united.parse(_stream(flight))
    business = check(parsed, watch("UA1", "ORD", "LAX", Cabin.BUSINESS, "united"))
    assert (business.status, business.miles) == (CheckStatus.AVAILABLE, 50_000)
    first = check(parsed, watch("UA1", "ORD", "LAX", Cabin.FIRST, "united"))
    assert first.status is CheckStatus.UNKNOWN and "Suite" in first.detail


async def test_format_warning_email_names_the_unknown_fare(store, clock):
    add_cx(store, clock, program="delta")
    slices = cx_slice()
    parsed = american.parse(to_html(response(slices)))
    item = parsed.slices[0]
    parsed.slices[0] = type(item)(legs=item.legs, offers=(), unknown_fares=("CNEWFARE",))
    answer = SearchAnswer(AnswerKind.ANSWER, parsed=parsed)
    runner, _, sender, _ = make_runner(store, clock, [answer, answer])
    await runner.tick()
    clock.now += 86400 / 3
    await runner.tick()
    assert "delta.com" in sender.subjects[0] and "CNEWFARE" in sender.sent[0][1]


def test_delta_says_no_offers_is_a_genuine_empty():
    body = json.dumps(
        {
            "errors": [
                {
                    "message": "RetailOfferError",
                    "extensions": {
                        "errors": {
                            "developerMessage": "Shopping API: Unable to retrieve Flight offers"
                        }
                    },
                }
            ],
            "data": {"gqlSearchOffers": None},
        }
    )
    assert classify(RawResponse(200, body), delta.MARKERS)[0] is Outcome.OK
    assert dl(body=body).status is CheckStatus.NOT_LISTED


def test_delta_other_error_is_drift_not_empty():
    body = json.dumps({"errors": [{"message": "Boom"}], "data": None})
    with pytest.raises(SchemaDrift):
        delta.parse(body)


def test_delta_partial_error_is_unknown():
    data = _delta_data()
    data["errors"] = [{"message": "Some offers failed"}]
    assert dl(body=json.dumps(data)).status is CheckStatus.UNKNOWN


def test_delta_missing_result_pages_are_unknown():
    data = _delta_data()
    props = data["data"]["gqlSearchOffers"]["offerDataList"]["responseProperties"]
    props["pageResultCnt"] = 3
    assert dl("DL999", body=json.dumps(data)).status is CheckStatus.UNKNOWN


def test_delta_multiple_pages_are_combined():
    first, second = _delta_data(), deepcopy(_delta_data())
    for page in (first, second):
        props = page["data"]["gqlSearchOffers"]["offerDataList"]["responseProperties"]
        props["pageResultCnt"] = 2
    second["data"]["gqlSearchOffers"]["gqlOffersSets"] = []
    assert dl(body=json.dumps([first, second])).miles == 499_900


@pytest.mark.parametrize("brand", ["KEPC", "CKEPC"])
def test_korean_air_prestige_is_business(brand):
    assert delta.brand_cabin(brand) is Cabin.BUSINESS


# ---- registry and links -------------------------------------------------------------------
def test_three_programs_each_with_a_budget():
    assert set(PROGRAMS) == {"american", "delta", "united"} == set(BUDGETS)


@pytest.mark.parametrize(
    ("text", "program"),
    [
        ("AA", "american"),
        ("american airlines", "american"),
        ("Delta", "delta"),
        ("skymiles", "delta"),
        ("UA", "united"),
        ("MileagePlus", "united"),
    ],
)
def test_program_names(text, program):
    assert parse_program(text) == program


def test_unknown_program_is_refused(store, clock):
    with pytest.raises(ValueError):
        parse_program("alaska")
    with pytest.raises(ValueError):
        create_watch(
            store,
            flight="UA1",
            origin="ORD",
            destination="LAX",
            dates="2027-05-21",
            cabin="first",
            program="alaska",
            now=clock(),
        )


@pytest.mark.parametrize(
    ("program", "domain"),
    [("american", "aa.com"), ("delta", "delta.com"), ("united", "united.com")],
)
def test_booking_links_use_the_real_party_size(program, domain):
    url = get_site(program).search_url(Query("JFK", "HKG", DAY, 3, program))
    assert domain in url and ("px=3" in url or "paxCount=3" in url or "pax=3" in url)


def test_searches_ask_for_the_real_party_size():
    assert len(united.search_request(Query("ORD", "HND", DAY, 3))["PaxInfoList"]) == 3
    criteria = delta.search_variables(Query("DTW", "ICN", DAY, 3))["offerSearchCriteria"]
    assert len(criteria["customers"]) == 3


def test_444_counts_as_a_block():
    assert classify(RawResponse(444, ""), delta.MARKERS)[0] is Outcome.BLOCKED


# ---- every request goes through the governor ----------------------------------------------
class FakePage:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def evaluate(self, _js, args):
        self.calls.append(args)
        return self.responses.pop(0)


class FakeBroker:
    def __init__(self, page):
        self.fake_page, self.opened, self.site = page, 0, None

    def is_open(self, site):
        return self.site == site

    async def page(self, site, _url):
        if self.site != site:
            self.opened += 1
            self.site = site
        return self.fake_page

    async def close(self):
        self.site = None


def live(store, clock, *responses):
    governor = Governor(store, clock, clock.sleep)
    page = FakePage(responses)
    broker = FakeBroker(page)
    return LiveFetcher(governor, broker), governor, page, broker


TOKEN = {"status": 200, "text": json.dumps({"data": {"token": {"hash": "abc"}}})}


@pytest.fixture
def no_raw_files(monkeypatch, tmp_path):
    monkeypatch.setenv("JETBRIDGE_HOME", str(tmp_path))


async def test_united_counts_page_load_token_and_search(store, clock):
    fetcher, governor, page, _ = live(store, clock, TOKEN, {"status": 200, "text": UNITED_BODY})
    answer = await fetcher.search(Query("ORD", "HND", RECORDED_DAY, 1, "united"), clock() + 60)
    assert answer.kind is AnswerKind.ANSWER and len(answer.parsed.slices) == 30
    assert governor.used("united") == 3 and governor.used("american") == 0
    assert "bearer abc" in page.calls[1]["headers"]["x-authorization-api"]


async def test_united_blocked_token_never_reaches_the_search(store, clock, no_raw_files):
    fetcher, governor, page, _ = live(store, clock, {"status": 403, "text": "no"})
    answer = await fetcher.search(Query("ORD", "HND", DAY, 1, "united"), clock() + 60)
    assert answer.kind is AnswerKind.BLOCKED and len(page.calls) == 1
    assert governor.cooldown_until("united") > clock() and not governor.cooldown_until("delta")


async def test_united_challenge_instead_of_token_is_a_block(store, clock, no_raw_files):
    fetcher, _, page, _ = live(store, clock, {"status": 200, "text": block_page()})
    answer = await fetcher.search(Query("ORD", "HND", DAY, 1, "united"), clock() + 60)
    assert answer.kind is AnswerKind.BLOCKED and len(page.calls) == 1


async def test_united_changed_token_shape_is_drift(store, clock, no_raw_files):
    fetcher, _, _, _ = live(store, clock, {"status": 200, "text": '{"data": {}}'})
    answer = await fetcher.search(Query("ORD", "HND", DAY, 1, "united"), clock() + 60)
    assert answer.kind is AnswerKind.DRIFT


async def test_empty_reply_is_unknown_without_a_format_warning(store, clock):
    fetcher, _, _, _ = live(store, clock, TOKEN, {"status": 200, "text": ""})
    query = Query("ORD", "HND", date(2027, 12, 30), 1, "united")
    answer = await fetcher.search(query, clock() + 60)
    assert answer.kind is AnswerKind.ERROR and "empty reply" in answer.detail
    w = watch("UA1", "ORD", "HND", Cabin.BUSINESS, "united")
    result = evaluate(w, query.date, answer)
    assert result.status is CheckStatus.UNKNOWN and not result.suspect_parser


def _delta_page(pages: int) -> dict:
    data = _delta_data()
    data["data"]["gqlSearchOffers"]["offerDataList"]["responseProperties"]["pageResultCnt"] = pages
    return {"status": 200, "text": json.dumps(data)}


async def test_delta_counts_every_result_page(store, clock):
    fetcher, governor, page, _ = live(store, clock, _delta_page(2), _delta_page(2))
    answer = await fetcher.search(Query("DTW", "ICN", RECORDED_DAY, 2, "delta"), clock() + 60)
    assert answer.kind is AnswerKind.ANSWER and governor.used("delta") == 3
    assert [c["headers"]["authorization"] for c in page.calls] == ["GUEST", "GUEST"]
    sent = json.loads(page.calls[1]["body"])["variables"]["offerSearchCriteria"]
    assert sent["offersCriteria"]["resultsPageNum"] == 2 and len(sent["customers"]) == 2


async def test_delta_running_out_of_budget_defers_and_stays_within_it(store, clock):
    limit = BUDGETS["delta"].max_requests
    for _ in range(limit - 2):  # room for the page load and one result page
        store.record_start("delta", clock())
    fetcher, governor, page, _ = live(store, clock, _delta_page(2))
    answer = await fetcher.search(Query("DTW", "ICN", RECORDED_DAY, 1, "delta"), clock() + 1)
    assert answer.kind is AnswerKind.DEFERRED and answer.retry_at
    assert len(page.calls) == 1 and governor.used("delta") == limit


async def test_a_resting_site_gets_no_request_not_even_a_page_load(store, clock):
    governor_only = Governor(store, clock, clock.sleep)
    governor_only.record_block("delta")
    fetcher, _, page, broker = live(store, clock)
    answer = await fetcher.search(Query("DTW", "ICN", DAY, 1, "delta"), clock() + 60)
    assert answer.kind is AnswerKind.DEFERRED and not page.calls and broker.opened == 0


async def test_american_navigation_fallback_is_counted(store, clock, no_raw_files):
    fetcher, governor, _, _ = live(store, clock, {"status": 200, "text": "<html>odd</html>"})

    async def navigate(_page, _q):
        return RawResponse(200, "<html>still odd</html>", tier="T3")

    site = SITES["american"]
    SITES["american"] = type(site)(**{**site.__dict__, "fallback": navigate})
    try:
        answer = await fetcher.search(Q, clock() + 60)
    finally:
        SITES["american"] = site
    assert answer.kind is AnswerKind.DRIFT and governor.used("american") == 3


# ---- sites are independent ----------------------------------------------------------------
OPEN = response(cx_slice(seats=2))
CLOSED = response(cx_slice(business_miles=None))


def blocked(until: float) -> SearchAnswer:
    return SearchAnswer(AnswerKind.BLOCKED, detail="challenge", retry_at=until)


async def test_a_block_on_one_airline_does_not_stop_the_others(store, clock):
    a = add_cx(store, clock, program="american")
    d = add_cx(store, clock, dates=(DAY, date(2027, 5, 22)), program="delta")
    u = add_cx(store, clock, program="united")
    runner, fetcher, _, _ = make_runner(
        store, clock, [answer_from(CLOSED), blocked(clock() + 600), answer_from(CLOSED)]
    )
    report = await runner.tick()

    assert [q.program for q in fetcher.queries] == ["american", "delta", "united"]
    assert "delta.com" in report.stopped_early
    assert store.date_state(a.id, DAY).status is CheckStatus.NOT_OFFERED
    assert store.date_state(u.id, DAY).status is CheckStatus.NOT_OFFERED
    assert store.date_state(d.id, DAY).status is None
    assert len(store.history(d.id)) == 2, "the skipped date still gets an outcome"
    assert store.get_watch(d.id).next_check_at >= clock() + 500
    assert store.get_watch(u.id).next_check_at >= clock() + 3600


async def test_same_route_on_two_programs_means_two_searches(store, clock):
    add_cx(store, clock, program="american")
    add_cx(store, clock, program="united")
    runner, fetcher, _, _ = make_runner(store, clock, [answer_from(CLOSED), answer_from(CLOSED)])
    await runner.tick()
    assert [q.program for q in fetcher.queries] == ["american", "united"]


async def test_no_wait_between_different_sites_but_a_wait_within_one(store, clock):
    add_cx(store, clock, program="american")
    add_cx(store, clock, program="delta")
    start = clock()
    runner, _, _, _ = make_runner(store, clock, [answer_from(CLOSED), answer_from(CLOSED)])
    await runner.tick()
    assert clock() == start


@pytest.mark.parametrize(("program", "domain"), [("delta", "delta.com"), ("united", "united.com")])
async def test_alert_links_to_the_watched_programs_site(store, clock, program, domain):
    add_cx(store, clock, program=program)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN)])
    await runner.tick()
    assert sender.subjects[0].startswith("SEATS OPEN")
    assert f"https://www.{domain}/" in sender.sent[0][1] and "aa.com" not in sender.sent[0][1]


async def test_page_format_warning_names_the_right_site(store, clock):
    add_cx(store, clock, program="united")
    drift = SearchAnswer(AnswerKind.DRIFT, detail="no meta event")
    runner, _, sender, _ = make_runner(store, clock, [drift, drift])
    await runner.tick()
    clock.now += 86400 / 3
    await runner.tick()
    assert sender.subjects == ["jetbridge: united.com's page format may have changed"]


async def test_clear_answer_on_one_site_does_not_clear_anothers_warning(store, clock):
    add_cx(store, clock, program="american")
    add_cx(store, clock, program="united")
    drift = SearchAnswer(AnswerKind.DRIFT, detail="x")
    answers = [answer_from(CLOSED), drift, answer_from(CLOSED), drift]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    clock.now += 86400 / 3
    await runner.tick()
    assert sender.subjects == ["jetbridge: united.com's page format may have changed"]


async def test_digest_names_the_program_and_resting_site(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock, program="delta")
    runner, _, sender, governor = make_runner(store, clock, [answer_from(CLOSED)])
    governor.record_block("united")
    await runner.tick()
    body = sender.sent[0][1]
    assert "via Delta SkyMiles" in body and "united.com is resting" in body
    assert american.SITE == "american"
