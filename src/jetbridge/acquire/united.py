"""United MileagePlus (united.com), searched as a guest with no login.

Recon (2026-10-31):
* ``POST /api/flight/FetchSSENestedFlights`` returns award pricing for a guest bearer token from
  ``GET /api/auth/anonymous-token``, called from inside a warmed page.
* The response is Server-Sent Events. ``flightOption`` events hold itineraries, and in recorded
  streams the last event is ``streamingTimings``.
* A product's ``cabinType`` describes the first flight; connecting flights' cabins are in
  ``connections[i].products`` under the same ``productType``. The product name isn't the seat:
  a "MIN-BUSINESS" product is often flown in a seat labelled First on domestic flights.
* ``bookingClassAvailability`` ("J9|JN9|I0") gives a seat count for the booked class, capped at 9.

``parse`` is a pure function of the response body so it can be tested on recorded fixtures.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal
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

SITE = "united"
WARMUP_URL = "https://www.united.com/en/us"
MARKER = '"type":"meta"'
_TOKEN_URL = "/api/auth/anonymous-token"
_SEARCH_URL = "/api/flight/FetchSSENestedFlights"
_LAST_EVENT = "streamingTimings"
_SEAT_CAP = 9
_TIME_FORMAT = "%Y-%m-%d %H:%M"
_LEG_CABIN = {
    "coach": Cabin.ECONOMY,
    "economy": Cabin.ECONOMY,
    "premiumplus": Cabin.PREMIUM,
    "premium economy": Cabin.PREMIUM,
    "business": Cabin.BUSINESS,
    "first": Cabin.FIRST,
}


def search_url(q: Query) -> str:
    """The prefilled united.com results link used in alerts."""
    return (
        "https://www.united.com/en/us/fsr/choose-flights?"
        f"f={q.origin}&t={q.destination}&d={q.date.isoformat()}&tt=1&at=1&sc=7&px={q.pax}"
        "&taxng=1&newHP=True&clm=7&st=bestmatches&tqp=A"
    )


def search_request(q: Query) -> dict[str, Any]:
    """The JSON the site posts (captured in recon 2026-10-31)."""
    d = q.date
    return {
        "SearchTypeSelection": 1,
        "SortType": "bestmatches",
        "SortTypeDescending": False,
        "Trips": [
            {
                "Origin": q.origin,
                "Destination": q.destination,
                "DepartDate": d.isoformat(),
                "Index": 1,
                "TripIndex": 1,
                "SearchRadiusMilesOrigin": 0,
                "SearchRadiusMilesDestination": 0,
                "DepartTimeApprox": 0,
                "SearchFiltersIn": {
                    "FareFamily": "ECONOMY",
                    "AirportsStop": None,
                    "AirportsStopToAvoid": None,
                    "ShopIndicators": {"IsTravelCreditsApplied": False, "IsDoveFlow": True},
                },
            }
        ],
        "CabinPreferenceMain": "economy",
        "PaxInfoList": [{"PaxType": 1} for _ in range(q.pax)],
        "AwardTravel": True,
        "NGRP": False,
        "CalendarLengthOfStay": 0,
        "PetCount": 0,
        "RecentSearchKey": f"{q.origin}{q.destination}{d.month}/{d.day}/{d.year}",
        "CalendarFilters": {"Filters": {"PriceScheduleOptions": {"Stops": 1}}},
        "Characteristics": [
            {"Code": "SOFT_LOGGED_IN", "Value": False},
            {"Code": "UsePassedCartId", "Value": False},
        ],
        "FareType": "Refundable",
        "BuildHashValue": "true",
    }


async def fetch(page: Page, q: Query, gate: Gate) -> RawResponse:
    """Two requests: the guest token, then the search. An unusable token response is returned
    as it is, so the normal classification decides whether it was a block."""
    await gate()
    token_response = await page_fetch(page, _TOKEN_URL)
    if token_response.status != 200:
        return token_response
    try:
        token = json.loads(token_response.body)["data"]["token"]["hash"]
    except (KeyError, TypeError, ValueError):
        return token_response

    await gate()
    return await page_fetch(
        page,
        _SEARCH_URL,
        method="POST",
        headers={
            "content-type": "application/json",
            "accept": "text/event-stream",
            "x-authorization-api": f"bearer {token}",
        },
        body=json.dumps(search_request(q)),
    )


def parse(body: str) -> ParsedResponse:
    """Parse a stream, raising ``SchemaDrift`` unless it's recognizably a search answer."""
    out = ParsedResponse()
    events: list[dict[str, Any]] = []
    for line in body.splitlines():
        if line.startswith("data: "):
            try:
                event = json.loads(line[6:])
            except ValueError as exc:
                out.unreadable.append(UnreadableSlice(frozenset(), f"undecodable event: {exc}"))
                continue
            if isinstance(event, dict):
                events.append(event)

    meta = next((e for e in events if e.get("type") == "meta"), None)
    if meta is None:
        raise SchemaDrift("no meta event in United's stream")
    if meta.get("status") not in (None, "success"):
        out.site_error = str(meta.get("status"))
    if not any(e.get("type") == _LAST_EVENT for e in events):
        # A cut-off stream could be missing the watched flight, so it can't mean "no seats".
        out.unreadable.append(UnreadableSlice(frozenset(), "stream ended early"))

    for event in events:
        if event.get("type") == "flightOption":
            flight = event.get("flight")
            try:
                out.slices.append(_parse_flight(flight))
            except (KeyError, TypeError, ValueError, AttributeError) as exc:
                out.unreadable.append(UnreadableSlice(_flight_hints(flight), repr(exc)))
    return out


def _parse_flight(flight: dict[str, Any]) -> ParsedSlice:
    segments = [flight, *flight.get("connections", [])]
    legs = tuple(_parse_leg(segment) for segment in segments)
    products = flight["products"]  # required: its absence must not read as "no seats"
    if not isinstance(products, list):
        raise ValueError("products is not a list")
    offers = []
    unknown = []
    for index, product in enumerate(products):
        try:
            offer = _parse_offer(product, index, segments)
        except UnknownFare as exc:
            unknown.append(exc.fare)
            continue
        if offer is not None:
            offers.append(offer)
    return ParsedSlice(legs=legs, offers=tuple(offers), unknown_fares=tuple(unknown))


def _parse_leg(segment: dict[str, Any]) -> Leg:
    return Leg(
        carrier=segment["marketingCarrier"],
        number=normalize_number(segment["flightNumber"]),
        origin=segment["origin"],
        destination=segment["destination"],
        departs_at=datetime.strptime(segment["departDateTime"], _TIME_FORMAT),
        arrives_at=datetime.strptime(segment["destinationDateTime"], _TIME_FORMAT),
        operating_carrier=segment.get("operatingCarrier") or None,
        equipment=(segment.get("equipmentDisclosures") or {}).get("equipmentType"),
    )


def _parse_offer(
    product: dict[str, Any], index: int, segments: list[dict[str, Any]]
) -> ProductOffer | None:
    prices = product.get("prices")
    if not prices:
        return None
    miles = next((int(p["amount"]) for p in prices if p.get("currency") == "MILES"), None)
    tax = next((p for p in prices if p.get("pricingType") == "Tax"), None)
    if not miles or tax is None:
        return None

    product_type = product.get("productType")
    cabins: list[Cabin] = []
    codes: list[str] = []
    for position, segment in enumerate(segments):
        leg_product = product if position == 0 else _same_product(segment, product_type, index)
        if leg_product is None or not leg_product.get("cabinType"):
            raise UnknownFare(str(product_type))
        cabin = _LEG_CABIN.get(leg_product["cabinType"].lower())
        if cabin is None:
            raise UnknownFare(leg_product["cabinType"])
        cabins.append(cabin)
        codes.append(leg_product.get("bookingCode") or "")

    exact, at_least = _seats(segments, codes)
    return ProductOffer(
        cabin=cabins[0],  # the matcher also requires every leg to be in the watched cabin
        leg_cabins=tuple(cabins),
        miles=miles,
        taxes=Decimal(str(tax["amount"])).quantize(Decimal("0.01")),
        currency=tax.get("currency") or "USD",
        seats_remaining=exact,
        seats_at_least=at_least,
    )


def _same_product(segment: dict[str, Any], product_type: Any, index: int) -> dict[str, Any] | None:
    products = segment.get("products") or []
    if index < len(products) and products[index].get("productType") == product_type:
        return products[index]
    return next((p for p in products if p.get("productType") == product_type), None)


def _seats(segments: list[dict[str, Any]], codes: list[str]) -> tuple[int, int]:
    """(exact count, capped "at least" count). (0, 0) means the site didn't say."""
    counts: list[int] = []
    for segment, code in zip(segments, codes, strict=True):
        available: dict[str, int] = {}
        for token in (segment.get("bookingClassAvailability") or "").split("|"):
            name = token.rstrip("0123456789")
            if name and name != token:
                available[name] = int(token[len(name) :])
        if code not in available:
            return 0, 0
        counts.append(available[code])
    fewest = min(counts, default=0)
    if fewest <= 0:
        return 0, 0
    return (0, _SEAT_CAP) if fewest >= _SEAT_CAP else (fewest, 0)


def _flight_hints(flight: Any) -> frozenset[str]:
    """Best-effort flight codes from an option that failed to parse. Empty means unknown."""
    codes: set[str] = set()
    try:
        for segment in [flight, *(flight.get("connections") or [])]:
            if segment.get("marketingCarrier") and segment.get("flightNumber"):
                number = normalize_number(segment["flightNumber"])
                codes.add(f"{segment['marketingCarrier']}{number}")
    except (AttributeError, TypeError):
        return frozenset()
    return frozenset(codes)
