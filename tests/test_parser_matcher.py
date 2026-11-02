from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from conftest import DAY, answer_from, block_page, cx_slice, response, to_html

from jetbridge.acquire import american
from jetbridge.acquire.detect import classify
from jetbridge.acquire.types import Outcome, RawResponse, SchemaDrift
from jetbridge.matcher import evaluate
from jetbridge.models import AnswerKind, Cabin, CheckStatus, Flight, SearchAnswer, Watch


def watch(**kw) -> Watch:
    base = dict(
        id=1,
        flight=Flight("CX", "805"),
        origin="JFK",
        destination="HKG",
        dates=(DAY,),
        cabin=Cabin.BUSINESS,
    )
    return Watch(**{**base, **kw})


def check(state, w=None, day=DAY):
    return evaluate(w or watch(), day, answer_from(state))


# ---- real recorded response -----------------------------------------------------------
def test_recorded_fixture_parses_fully(aa_state):
    parsed = american.parse(to_html(aa_state))
    assert len(parsed.slices) == 40 and not parsed.unreadable and parsed.site_error is None


def test_recorded_fixture_finds_real_nonstop(aa_state):
    w = watch(flight=Flight("AA", "175"), origin="DFW", destination="HND")
    result = check(aa_state, w, date(2027, 5, 28))
    assert result.status is CheckStatus.AVAILABLE
    assert result.miles == 363_500
    assert result.seats_exact is None and result.seats_at_least == 1  # seatsRemaining 0 = unknown


def test_recorded_fixture_excludes_connection_with_same_flight(aa_state):
    # AA27 only appears on DFW connections here, so a DFW-HND watch isn't that flight.
    w = watch(flight=Flight("AA", "27"), origin="DFW", destination="HND")
    assert check(aa_state, w, date(2027, 5, 28)).status is CheckStatus.NOT_LISTED


# ---- matching -------------------------------------------------------------------------
def test_available():
    result = check(response(cx_slice(seats=2)))
    assert result.status is CheckStatus.AVAILABLE
    assert (result.miles, result.seats_exact, result.seats_text) == (110_000, 2, "2")


def test_flight_number_with_leading_zeros_matches():
    assert check(response(cx_slice(flight="0805"))).status is CheckStatus.AVAILABLE


def test_listed_without_business_is_not_offered():
    assert check(response(cx_slice(business_miles=None))).status is CheckStatus.NOT_OFFERED


def test_business_product_flown_in_another_cabin_does_not_count():
    assert check(response(cx_slice(leg_cabin="COACH"))).status is CheckStatus.NOT_OFFERED


def test_other_flight_is_not_listed():
    assert check(response(cx_slice(flight="889"))).status is CheckStatus.NOT_LISTED


def test_other_date_is_not_listed():
    assert check(response(cx_slice(day="2027-05-22"))).status is CheckStatus.NOT_LISTED


def test_positive_empty_answer_is_not_listed():
    assert check(response()).status is CheckStatus.NOT_LISTED


def test_price_above_limit_is_not_offered():
    result = check(response(cx_slice(business_miles=200_000)), watch(max_miles=150_000))
    assert result.status is CheckStatus.NOT_OFFERED and "above your limit" in result.detail


def test_price_at_limit_is_available():
    result = check(response(cx_slice(business_miles=150_000)), watch(max_miles=150_000))
    assert result.status is CheckStatus.AVAILABLE


# ---- never "no seats" unless positively known -------------------------------------------
def test_site_error_code_is_unknown():
    assert check(response(error="309")).status is CheckStatus.UNKNOWN


def test_unreadable_slice_for_watched_flight_is_unknown():
    damaged = cx_slice()
    del damaged["pricingDetail"]
    result = check(response(damaged))
    assert result.status is CheckStatus.UNKNOWN and result.suspect_parser


def test_unreadable_slice_for_other_flight_does_not_hide_the_answer():
    damaged = cx_slice(flight="889")
    del damaged["pricingDetail"]
    assert check(response(damaged, cx_slice())).status is CheckStatus.AVAILABLE


def test_slice_with_unrecoverable_identity_is_unknown():
    assert check(response({"segments": "garbage"})).status is CheckStatus.UNKNOWN


@pytest.mark.parametrize(
    "kind", [AnswerKind.BLOCKED, AnswerKind.DRIFT, AnswerKind.ERROR, AnswerKind.DEFERRED]
)
def test_every_non_answer_is_unknown(kind):
    result = evaluate(watch(), DAY, SearchAnswer(kind, detail="x"))
    assert result.status is CheckStatus.UNKNOWN
    assert result.suspect_parser is (kind is AnswerKind.DRIFT)


@pytest.mark.parametrize(
    "body",
    [
        "<html>no state here</html>",
        '<script id="ng-state">not json</script>',
        '<script id="ng-state">{"other": 1}</script>',
        '<script id="ng-state">{"SearchData": {"itineraryResult": {"slices": []}}}</script>',
    ],
)
def test_unrecognized_bodies_raise_drift(body):
    with pytest.raises(SchemaDrift):
        american.parse(body)


# ---- block detection ------------------------------------------------------------------
@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (200, block_page(), Outcome.BLOCKED),
        (403, "nope", Outcome.BLOCKED),
        (429, "slow down", Outcome.RATE_LIMITED),
        (500, "oops", Outcome.ERROR),
        (None, "failed", Outcome.ERROR),
        (200, "<html>something else</html>", Outcome.ERROR),
        (200, to_html(response()), Outcome.OK),
    ],
)
def test_classify(status, body, expected):
    assert classify(RawResponse(status, body), american.MARKER)[0] is expected


def test_booking_link_uses_real_party_size():
    from jetbridge.models import Query

    url = american.search_url(Query("JFK", "HKG", DAY, pax=3))
    assert "pax=3" in url and "searchType=Award" in url
    assert american.search_request(Query("JFK", "HKG", DAY, pax=3))["passengers"][0]["count"] == 3


# ---- real CX805 response recorded 2026-11-01: economy open, no business ----------------
def _cx805(cabin: Cabin):
    path = Path(__file__).parent / "fixtures" / "american" / "yyz_hkg_award_20270521.html"
    answer = SearchAnswer(
        AnswerKind.ANSWER, parsed=american.parse(path.read_text(encoding="utf-8"))
    )
    return evaluate(watch(origin="YYZ", cabin=cabin), DAY, answer)


def test_recorded_cx805_has_no_business():
    assert _cx805(Cabin.BUSINESS).status is CheckStatus.NOT_OFFERED


def test_recorded_cx805_has_economy():
    result = _cx805(Cabin.ECONOMY)
    assert (result.status, result.miles) == (CheckStatus.AVAILABLE, 37_500)
