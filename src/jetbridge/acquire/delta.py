"""Delta SkyMiles (delta.com), searched as a guest with no login.

Recon (2026-10-31):
* ``POST https://offer-api-prd.delta.com/prd/rm-offer-gql`` (GraphQL) with the header
  ``authorization: GUEST`` returns award offers, called from inside a warmed delta.com page.
* Results come in pages. Each offer set is one itinerary, and its offers are the brand columns,
  with ``availableSeatCnt`` (capped at 9) and the actual per-leg brand in ``brandByFlightLegs``.
* Delta says "RetailOfferError ... Unable to retrieve Flight offers" when a market has no offers,
  which is a genuine empty answer. Any other error isn't.
* Timestamps are airport-local with no offset, and are kept that way, because the watched date
  is the local departure date.

``parse`` is a pure function of the response body so it can be tested on recorded fixtures.
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jetbridge.acquire.pagefetch import Gate, page_fetch
from jetbridge.acquire.types import RawResponse, SchemaDrift
from jetbridge.models import (
    Cabin,
    Leg,
    ParsedResponse,
    ParsedSlice,
    ProductOffer,
    Query,
    UnreadableSlice,
    normalize_number,
)

if TYPE_CHECKING:
    from patchright.async_api import Page

SITE = "delta"
WARMUP_URL = "https://www.delta.com/"
ENDPOINT = "https://offer-api-prd.delta.com/prd/rm-offer-gql"
MARKERS = ('"gqlSearchOffers"', "RetailOfferError")
MAX_PAGES = 10
_SEAT_CAP = 9
_NO_OFFERS = "Unable to retrieve Flight offers"
_RESULTS_QUERY = Path(__file__).parent / "gql" / "delta_results.graphql"

# Brand ids can be prefixed B or C by fare type.
_BRAND_CABIN = {
    "MAIN": Cabin.ECONOMY,  # Main and Basic
    "DCP": Cabin.ECONOMY,  # Comfort: economy cabin with extra legroom
    "DPPS": Cabin.PREMIUM,  # Premium Select
    "DPS": Cabin.PREMIUM,
    "AFPE": Cabin.PREMIUM,  # Air France: labelled AWARD-PREMIUM-PLUS, in the Premium column
    "PE": Cabin.PREMIUM,
    "D1": Cabin.BUSINESS,  # Delta One
    "FIRST": Cabin.FIRST,  # domestic First
    "BU": Cabin.BUSINESS,  # partner business
    "KEEC": Cabin.ECONOMY,  # Korean Air economy award class
    "KEPC": Cabin.BUSINESS,  # Korean Air Prestige award class
}


class UnknownBrand(ValueError):
    """A fare type we can't place in a cabin. Only that fare is affected, not the itinerary."""

    def __init__(self, brand: str):
        super().__init__(f"unknown Delta brand id {brand!r}")
        self.brand = brand


def brand_cabin(brand: str) -> Cabin:
    upper = brand.upper()
    for candidate in (upper, upper[1:]):
        if candidate in _BRAND_CABIN:
            return _BRAND_CABIN[candidate]
    raise UnknownBrand(brand)


def search_url(q: Query) -> str:
    """The prefilled delta.com results link used in alerts."""
    return (
        "https://www.delta.com/flight-search/search?action=findFlights&tripType=ONE_WAY"
        f"&originCity={q.origin}&destinationCity={q.destination}"
        f"&departureDate={q.date.strftime('%m/%d/%Y')}&paxCount={q.pax}&awardTravel=true"
    )


def search_variables(q: Query, page_number: int = 1) -> dict[str, Any]:
    """The GraphQL variables the site sends (captured in recon 2026-10-31)."""
    return {
        "offerSearchCriteria": {
            "productGroups": [{"productCategoryCode": "FLIGHTS"}],
            "offersCriteria": {
                "resultsPageNum": page_number,
                "resultsPerRequestNum": 20,
                "preferences": {
                    "refundableOnly": False,
                    "showGlobalRegionalUpgradeCertificate": True,
                    "nonStopOnly": False,
                    "excludeBrandTypes": [],
                },
                "pricingCriteria": {"priceableIn": ["MILES"]},
                "flightRequestCriteria": {
                    "currentTripIndexId": "0",
                    "sortableOptionId": None,
                    "selectedOfferId": "",
                    "searchOriginDestination": [
                        {
                            "departureLocalTs": f"{q.date.isoformat()}T00:00:00",
                            "destinations": [{"airportCode": q.destination}],
                            "origins": [{"airportCode": q.origin}],
                        }
                    ],
                    "sortByBrandId": "BE",
                    "additionalCriteriaMap": {"rollOutTag": "GBB"},
                },
            },
            "customers": [
                {"passengerTypeCode": "ADT", "passengerId": str(i + 1)} for i in range(q.pax)
            ],
        }
    }


def _page_count(body: str) -> int:
    try:
        props = json.loads(body)["data"]["gqlSearchOffers"]["offerDataList"]["responseProperties"]
        return int(props.get("pageResultCnt") or 1)
    except (AttributeError, KeyError, TypeError, ValueError):
        return 1


async def fetch(page: Page, q: Query, gate: Gate) -> RawResponse:
    """Every page of results, each its own request. The body is a JSON array of the pages."""
    transaction = str(uuid.uuid4())
    query = _RESULTS_QUERY.read_text(encoding="utf-8")
    bodies: list[str] = []
    number, total = 1, 1
    first: RawResponse | None = None
    while number <= total and number <= MAX_PAGES:
        await gate()
        raw = await page_fetch(
            page,
            ENDPOINT,
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "application/json, text/plain, */*",
                "airline": "DL",
                "applicationid": "DC",
                "authorization": "GUEST",
                "channelid": "DCOM",
                "transactionid": f"{transaction}_{int(time.time() * 1000)}",
                "x-app-type": "dcom-shop",
                "x-app-route": "SL-RSB",
            },
            body=json.dumps({"query": query, "variables": search_variables(q, number)}),
        )
        if raw.status != 200:
            return raw  # including a later page: a partial answer can't be trusted
        first = first or raw
        bodies.append(raw.body)
        total = max(total, _page_count(raw.body))
        number += 1
    assert first is not None
    first.body = "[" + ",".join(bodies) + "]"
    return first


def _is_no_offers(data: dict[str, Any]) -> bool:
    errors = data.get("errors") or []
    if not errors or (data.get("data") or {}).get("gqlSearchOffers"):
        return False
    return all(
        error.get("message") == "RetailOfferError"
        and _NO_OFFERS
        in str(((error.get("extensions") or {}).get("errors") or {}).get("developerMessage", ""))
        for error in errors
    )


def parse(body: str) -> ParsedResponse:
    """Parse the pages, raising ``SchemaDrift`` unless they're recognizably a search answer."""
    try:
        decoded = json.loads(body)
    except ValueError as exc:
        raise SchemaDrift(f"Delta's response isn't JSON: {exc}") from exc
    pages = decoded if isinstance(decoded, list) else [decoded]
    if not pages or not all(isinstance(page, dict) for page in pages):
        raise SchemaDrift("Delta's response isn't a list of result pages")

    out = ParsedResponse()
    expected = 1
    for data in pages:
        if _is_no_offers(data):
            continue
        result = (data.get("data") or {}).get("gqlSearchOffers")
        errors = data.get("errors")
        if errors:
            messages = "; ".join(str(e.get("message"))[:200] for e in errors)
            if not result:
                raise SchemaDrift(f"Delta GraphQL errors: {messages}")
            out.site_error = messages  # some offers may be missing, so it can't mean "no seats"
        if result is None:
            raise SchemaDrift("Delta's response has no gqlSearchOffers")
        expected = max(expected, _page_count(json.dumps(data)))
        for offer_set in result.get("gqlOffersSets") or []:
            try:
                out.slices.append(_parse_set(offer_set))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                out.unreadable.append(UnreadableSlice(_flight_hints(offer_set), repr(exc)))
    if len(pages) < expected:
        out.unreadable.append(
            UnreadableSlice(frozenset(), f"only {len(pages)} of {expected} result pages")
        )
    return out


def _legs_of(offer_set: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    return [
        (segment, leg)
        for trip in offer_set["trips"]
        for segment in trip["flightSegment"]
        for leg in segment["flightLeg"]
    ]


def _parse_set(offer_set: dict[str, Any]) -> ParsedSlice:
    pairs = _legs_of(offer_set)
    if not pairs:
        raise ValueError("itinerary has no legs")
    legs = tuple(_parse_leg(segment, leg) for segment, leg in pairs)
    offers: list[ProductOffer] = []
    unknown: list[str] = []
    for offer in offer_set.get("offers") or []:
        properties = offer.get("additionalOfferProperties") or {}
        if offer.get("soldOut") or properties.get("soldOut"):
            continue
        for item in offer.get("offerItems") or []:
            for retail in item.get("retailItems") or []:
                metadata = retail.get("retailItemMetaData") or {}
                for fare in metadata.get("fareInformation") or []:
                    try:
                        parsed = _parse_fare(fare, len(legs))
                    except UnknownBrand as exc:
                        unknown.append(exc.brand)
                        continue
                    if parsed is not None:
                        offers.append(parsed)
    return ParsedSlice(legs=legs, offers=tuple(offers), unknown_fares=tuple(unknown))


def _parse_leg(segment: dict[str, Any], leg: dict[str, Any]) -> Leg:
    marketing = segment["marketingCarrier"]
    operating = leg.get("operatingCarrier") or segment.get("operatingCarrier") or {}
    return Leg(
        carrier=marketing["carrierCode"],
        number=normalize_number(marketing["carrierNum"]),
        origin=leg["originAirportCode"],
        destination=leg["destinationAirportCode"],
        departs_at=datetime.fromisoformat(leg["scheduledDepartureLocalTs"]),
        arrives_at=datetime.fromisoformat(leg["scheduledArrivalLocalTs"]),
        operating_carrier=operating.get("carrierCode"),
        equipment=(leg.get("aircraft") or {}).get("fleetTypeCode"),
    )


def _parse_fare(fare: dict[str, Any], leg_count: int) -> ProductOffer | None:
    prices = fare.get("farePrice") or []
    if not prices:
        return None
    total = prices[0]["totalFarePrice"]
    miles = (total.get("milesEquivalentPrice") or {}).get("mileCnt")
    if not miles:
        return None
    tax_text = (total.get("currencyEquivalentPrice") or {}).get("formattedCurrencyAmt")
    if tax_text is None:
        raise ValueError("Delta fare has no taxes")
    brands = fare.get("brandByFlightLegs") or []
    if len(brands) != leg_count:
        raise ValueError(f"{len(brands)} brands for {leg_count} legs")
    if not all(brand.get("brandId") for brand in brands):
        return None  # this column isn't offered on every leg
    cabins = tuple(brand_cabin(brand["brandId"]) for brand in brands)

    count = fare.get("availableSeatCnt")
    exact, at_least = 0, 0
    if isinstance(count, int) and count >= _SEAT_CAP:
        at_least = _SEAT_CAP
    elif isinstance(count, int) and count > 0:
        exact = count
    return ProductOffer(
        cabin=cabins[0],  # the matcher also requires every leg to be in the watched cabin
        leg_cabins=cabins,
        miles=int(miles),
        taxes=Decimal(str(tax_text).replace(",", "").replace("$", "").strip()).quantize(
            Decimal("0.01")
        ),
        currency="USD",
        seats_remaining=exact,
        seats_at_least=at_least,
    )


def _flight_hints(offer_set: Any) -> frozenset[str]:
    """Best-effort flight codes from an itinerary that failed to parse. Empty means unknown."""
    codes: set[str] = set()
    try:
        for trip in offer_set.get("trips") or []:
            for segment in trip.get("flightSegment") or []:
                marketing = segment.get("marketingCarrier") or {}
                if marketing.get("carrierCode") and marketing.get("carrierNum"):
                    number = normalize_number(marketing["carrierNum"])
                    codes.add(f"{marketing['carrierCode']}{number}")
    except (AttributeError, TypeError):
        return frozenset()
    return frozenset(codes)
