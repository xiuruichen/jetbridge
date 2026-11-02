"""Delta SkyMiles (delta.com), searched as a guest with no login.

Recon (2026-10-31):
* ``POST https://offer-api-prd.delta.com/prd/rm-offer-gql`` (GraphQL) with the header
  ``authorization: GUEST`` returns award offers, called from inside a warmed delta.com page.
* Results come in pages. Each offer set is one itinerary, and its offers are the brand columns,
  with ``availableSeatCnt`` (capped at 9) and the actual per-leg brand in ``brandByFlightLegs``.
* Partner fares have their own brand ids (AFPE, KLBU, WSEC...), and new ones can appear at any
  time, but Delta places every fare in one of its own cabin columns. ``learn_brands`` works out
  each unfamiliar brand's cabin from that, using only the response itself.
* Delta says "RetailOfferError ... Unable to retrieve Flight offers" when a market has no offers,
  which is a genuine empty answer. Any other error isn't.
* Timestamps are airport-local with no offset, and are kept that way, because the watched date
  is the local departure date.

``parse`` is a pure function of the response body so it can be tested on recorded fixtures.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from jetbridge.acquire.pagefetch import Gate, page_fetch
from jetbridge.acquire.types import RawResponse, SchemaDrift, UnknownFare
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

# Delta's own brands, plus partner brands confirmed by hand. Other partners are learned from each
# response. Brand ids can be prefixed B or C by fare type.
_BRAND_CABIN = {
    "MAIN": Cabin.ECONOMY,  # Main and Basic
    "DCP": Cabin.ECONOMY,  # Comfort: economy cabin with extra legroom
    "DPPS": Cabin.PREMIUM,  # Premium Select
    "DPS": Cabin.PREMIUM,
    "PE": Cabin.PREMIUM,
    "D1": Cabin.BUSINESS,  # Delta One
    "FIRST": Cabin.FIRST,  # domestic First
    "BU": Cabin.BUSINESS,  # partner business
    "KEEC": Cabin.ECONOMY,  # Korean Air economy award class
    "KEPC": Cabin.BUSINESS,  # Korean Air Prestige award class
}


# What Delta's columns and award labels have meant in every response seen so far (2026-10-31 and
# 2026-11-01). Only used when a response doesn't show it, because it's all partner flights.
_USUAL_COLUMNS = {
    1: Cabin.ECONOMY,
    2: Cabin.ECONOMY,
    4: Cabin.ECONOMY,
    5: Cabin.PREMIUM,
    8: Cabin.BUSINESS,
}
_USUAL_LABELS = {
    "AWARD-BASIC": Cabin.ECONOMY,
    "AWARD-COMFORT-PLUS-INT": Cabin.ECONOMY,
    "AWARD-PREMIUM-PLUS": Cabin.PREMIUM,
    "AWARD-BUSINESS-FIRST": Cabin.BUSINESS,
}
_FARE_LABEL = re.compile(r"([A-Z0-9]+):(AWARD[A-Z-]*)")


def _known_cabin(brand: str) -> Cabin | None:
    upper = brand.upper()
    for candidate in (upper, upper[1:]):
        if candidate in _BRAND_CABIN:
            return _BRAND_CABIN[candidate]
    return None


def brand_cabin(brand: str, learned: dict[str, Cabin] | None = None) -> Cabin:
    cabin = _known_cabin(brand) or (learned or {}).get(brand)
    if cabin is None:
        raise UnknownFare(brand)
    return cabin


def _fares_of(offer: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        fare
        for item in offer.get("offerItems") or []
        for retail in item.get("retailItems") or []
        for fare in (retail.get("retailItemMetaData") or {}).get("fareInformation") or []
    ]


def _labels_of(fare: dict[str, Any]) -> dict[str, str]:
    """The award label Delta puts in a fare's solution id, by brand: AFPE -> AWARD-PREMIUM-PLUS."""
    return dict(_FARE_LABEL.findall(str(fare.get("solutionId") or "")))


def _label_for(brand: str, labels: dict[str, str]) -> str | None:
    return labels.get(brand) or labels.get(brand[1:])


def learn_brands(pages: list[dict[str, Any]]) -> dict[str, Cabin]:
    """Work out unfamiliar brands' cabins from how this response treats Delta's own brands.

    A column's main fare is named by ``dominantSegmentBrandId``. If that column only ever holds one
    cabin of Delta's own, an unfamiliar brand heading it is in that cabin. Other fares in a column
    say nothing, because a connection can put a cheaper cabin on one leg of a business column. The
    award label is a second opinion. Evidence in the response beats what's usually true, which only
    fills in for responses that are all partner flights. If the evidence disagrees, or there's none,
    the brand stays unknown: a wrong cabin would send a false alert.
    """
    offers = [
        offer
        for page in pages
        for offer_set in ((page.get("data") or {}).get("gqlSearchOffers") or {}).get(
            "gqlOffersSets"
        )
        or []
        for offer in offer_set.get("offers") or []
    ]

    column_cabins: dict[Any, set[Cabin]] = {}
    label_cabins: dict[str, set[Cabin]] = {}
    for offer in offers:
        properties = offer.get("additionalOfferProperties") or {}
        cabin = _known_cabin(str(properties.get("dominantSegmentBrandId") or ""))
        if cabin is not None:
            column_cabins.setdefault(properties.get("offerPriorityNum"), set()).add(cabin)
        for fare in _fares_of(offer):
            labels = _labels_of(fare)
            for leg in fare.get("brandByFlightLegs") or []:
                brand = leg.get("brandId") or ""
                known, label = _known_cabin(brand), _label_for(brand, labels)
                if known is not None and label is not None:
                    label_cabins.setdefault(label, set()).add(known)

    evidence: dict[str, set[Cabin | None]] = {}
    for offer in offers:
        properties = offer.get("additionalOfferProperties") or {}
        brand = properties.get("dominantSegmentBrandId")
        if not brand or _known_cabin(brand) is not None:
            continue
        opinions: set[Cabin] = set()
        priority = properties.get("offerPriorityNum")
        column = column_cabins.get(priority)
        if column is None and priority in _USUAL_COLUMNS:
            column = {_USUAL_COLUMNS[priority]}
        if column is not None and len(column) == 1:
            opinions |= column
        for fare in _fares_of(offer):
            label = _label_for(brand, _labels_of(fare))
            cabins = label_cabins.get(label or "")
            if cabins is None and label in _USUAL_LABELS:
                cabins = {_USUAL_LABELS[label]}
            if cabins is not None and len(cabins) == 1:
                opinions |= cabins
        evidence.setdefault(brand, set()).update(opinions if len(opinions) <= 1 else {None})

    learned: dict[str, Cabin] = {}
    for brand, opinions in evidence.items():
        if len(opinions) == 1 and None not in opinions:
            learned[brand] = next(iter(opinions))  # type: ignore[assignment]
    return learned


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
    learned = learn_brands(pages)
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
                out.slices.append(_parse_set(offer_set, learned))
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


def _parse_set(offer_set: dict[str, Any], learned: dict[str, Cabin]) -> ParsedSlice:
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
        for fare in _fares_of(offer):
            try:
                parsed = _parse_fare(fare, len(legs), learned)
            except UnknownFare as exc:
                unknown.append(exc.fare)
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


def _parse_fare(
    fare: dict[str, Any], leg_count: int, learned: dict[str, Cabin]
) -> ProductOffer | None:
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
    cabins = tuple(brand_cabin(brand["brandId"], learned) for brand in brands)

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
