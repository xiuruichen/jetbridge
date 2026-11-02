"""American AAdvantage (aa.com), searched as a guest with no login.

Recon (2026-10-31):
* The deep link ``/booking/search?...searchType=Award`` auto-submits a form
  ``POST /booking/choose-flights/1`` with ``searchRequest=<json>``. The response is
  server-rendered HTML embedding the result in ``<script id="ng-state">{...}</script>``.
* ``SearchData.itineraryResult.slices[]`` holds itineraries, with ``pricingDetail[]`` per
  product and the actual per-leg cabin in ``legs[].productDetails``.
* ``seatsRemaining`` is 0 when unknown (NOT sold out).
* The meaning of aa.com's error codes (e.g. "309") is unverified, so any error code is
  treated as "could not tell", never as "no flights".

``parse`` is a pure function of the response body so it can be tested on recorded fixtures.
"""

from __future__ import annotations

import json
import re
import urllib.parse
import uuid
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from jetbridge.acquire.detect import CHALLENGE_RE
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

SITE = "american"
WARMUP_URL = "https://www.aa.com/homePage.do"
MARKER = 'id="ng-state"'
REQUEST_TIMEOUT_MS = 90_000

_PRODUCT_CABIN = {
    "COACH": Cabin.ECONOMY,
    "PREMIUM_ECONOMY": Cabin.PREMIUM,
    "BUSINESS": Cabin.BUSINESS,
    "FIRST": Cabin.FIRST,
}
_NG_STATE = re.compile(r'<script[^>]*id="ng-state"[^>]*>(.*?)</script>', re.S)

# Page-context fetch: it uses the page's real cookies and browser networking.
FETCH_JS = """async ({url, method, headers, body, timeoutMs}) => {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), timeoutMs);
  try {
    const r = await fetch(url, {method, headers, body, credentials: 'include', signal: ctl.signal});
    return {status: r.status, text: await r.text()};
  } catch (e) {
    return {status: null, text: String(e)};
  } finally { clearTimeout(t); }
}"""


def search_url(q: Query) -> str:
    """The prefilled aa.com results link used in alerts and by the navigation fallback."""
    slices = [
        {
            "orig": q.origin,
            "origNearby": False,
            "dest": q.destination,
            "destNearby": False,
            "date": q.date.isoformat(),
        }
    ]
    params = {
        "locale": "en_US",
        "fareType": "Lowest",
        "pax": str(q.pax),
        "adult": str(q.pax),
        "type": "OneWay",
        "searchType": "Award",
        "cabin": "",
        "carriers": "ALL",
        "travelType": "personal",
        "slices": json.dumps(slices, separators=(",", ":")),
    }
    return "https://www.aa.com/booking/search?" + urllib.parse.urlencode(params)


def search_request(q: Query) -> dict[str, Any]:
    """The JSON the site posts as ``searchRequest`` (captured in recon 2026-10-31)."""
    return {
        "metadata": {"selectedProducts": [], "tripType": "OneWay", "udo": {}},
        "passengers": [{"type": "adult", "count": q.pax}],
        "requestHeader": {"clientId": "AAcom"},
        "slices": [
            {
                "allCarriers": True,
                "cabin": "",
                "departureDate": q.date.isoformat(),
                "destination": q.destination,
                "destinationNearbyAirports": False,
                "maxStops": None,
                "origin": q.origin,
                "originNearbyAirports": False,
            }
        ],
        "tripOptions": {
            "corporateBooking": False,
            "fareType": "Lowest",
            "locale": "en_US",
            "pointOfSale": None,
            "searchType": "Award",
            "enableBenefits": True,
        },
        "loyaltyInfo": None,
        "version": "",
        "queryParams": {
            "sliceIndex": 0,
            "sessionId": "",
            "solutionSet": "",
            "solutionId": "",
            "sort": "CARRIER",
        },
    }


async def fetch_form_post(page: Page, q: Query) -> RawResponse:
    """Tier T1: post the same form the site submits, from inside the page."""
    result = await page.evaluate(
        FETCH_JS,
        {
            "url": f"/booking/choose-flights/1?sid={uuid.uuid4()}&locale=en_US",
            "method": "POST",
            "headers": {
                "content-type": "application/x-www-form-urlencoded",
                "accept": "text/html",
            },
            "body": "searchRequest="
            + urllib.parse.quote(json.dumps(search_request(q), separators=(",", ":"))),
            "timeoutMs": REQUEST_TIMEOUT_MS,
        },
    )
    return RawResponse(status=result["status"], body=result["text"] or "", tier="T1")


async def fetch_navigate(page: Page, q: Query) -> RawResponse:
    """Tier T3: load the deep link in a throwaway tab and capture the form post."""
    tab = await page.context.new_page()
    try:
        async with tab.expect_response(
            lambda r: "/booking/choose-flights/" in r.url and r.request.method == "POST",
            timeout=45_000,
        ) as info:
            await tab.goto(search_url(q), wait_until="commit", timeout=REQUEST_TIMEOUT_MS)
        response = await info.value
        return RawResponse(status=response.status, body=await response.text(), tier="T3")
    except Exception as exc:
        # aa.com can show its human check BEFORE the form posts, so nothing is captured.
        try:
            html = await tab.content()
        except Exception:
            html = ""
        if CHALLENGE_RE.search(html[:50_000]):
            return RawResponse(status=200, body=html, tier="T3")
        return RawResponse(status=None, body=str(exc), tier="T3")
    finally:
        await tab.close()


def extract_state(body: str) -> dict[str, Any]:
    match = _NG_STATE.search(body)
    if not match:
        raise SchemaDrift("aa.com response has no ng-state transfer state")
    try:
        state = json.loads(match.group(1))
    except ValueError as exc:
        raise SchemaDrift(f"ng-state is not valid JSON: {exc}") from exc
    if not isinstance(state, dict):
        raise SchemaDrift("ng-state is not an object")
    return state


def parse(body: str) -> ParsedResponse:
    """Parse a response, raising ``SchemaDrift`` unless it's recognizably a search answer."""
    data = extract_state(body).get("SearchData")
    if not isinstance(data, dict) or not isinstance(data.get("itineraryResult"), dict):
        raise SchemaDrift("ng-state missing SearchData.itineraryResult")
    result = data["itineraryResult"]
    raw_slices = result.get("slices")
    if raw_slices is not None and not isinstance(raw_slices, list):
        raise SchemaDrift("itineraryResult.slices is not a list")

    error = result.get("error")
    out = ParsedResponse(site_error=str(error) if error else None)
    if not raw_slices and not error and result.get("responseMetadata") is None:
        # Zero results only count as genuine when the search metadata came back too.
        raise SchemaDrift("empty itineraryResult with no responseMetadata")

    for raw in raw_slices or []:
        try:
            out.slices.append(_parse_slice(raw))
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            out.unreadable.append(UnreadableSlice(_flight_hints(raw), repr(exc)))
    return out


def _parse_slice(raw: dict[str, Any]) -> ParsedSlice:
    pairs = [(seg, leg) for seg in raw["segments"] for leg in seg["legs"]]
    if not pairs:
        raise ValueError("slice has no legs")
    legs = tuple(_parse_leg(seg, leg) for seg, leg in pairs)
    details = raw["pricingDetail"]  # required: its absence must not read as "no seats"
    if not isinstance(details, list):
        raise ValueError("pricingDetail is not a list")
    return ParsedSlice(
        legs=legs,
        offers=tuple(
            offer for detail in details if (offer := _parse_offer(detail, pairs)) is not None
        ),
    )


def _parse_leg(seg: dict[str, Any], leg: dict[str, Any]) -> Leg:
    flight = leg.get("flight") or seg["flight"]
    marketing = flight["carrierCode"]
    return Leg(
        carrier=marketing,
        number=normalize_number(flight["flightNumber"]),
        origin=leg["origin"]["code"],
        destination=leg["destination"]["code"],
        departs_at=datetime.fromisoformat(leg["departureDateTime"]),
        arrives_at=datetime.fromisoformat(leg["arrivalDateTime"]),
        operating_carrier=_operating(leg, marketing),
        equipment=leg.get("aircraftCode"),
    )


def _parse_offer(
    detail: dict[str, Any], pairs: list[tuple[dict[str, Any], dict[str, Any]]]
) -> ProductOffer | None:
    product = detail.get("productType")
    cabin = _PRODUCT_CABIN.get(product)
    if cabin is None or not detail.get("productAvailable"):
        return None
    miles = detail.get("perPassengerAwardPoints")
    if not miles:
        return None

    leg_cabins = []
    for _, leg in pairs:
        leg_product = next(
            (p for p in leg.get("productDetails") or [] if p.get("productType") == product),
            None,
        )
        if leg_product is None or leg_product.get("cabinType") not in _PRODUCT_CABIN:
            raise ValueError(f"no cabin for {product} on leg {leg.get('origin', {}).get('code')}")
        leg_cabins.append(_PRODUCT_CABIN[leg_product["cabinType"]])

    tax = detail["perPassengerTaxesAndFees"]
    notes = []
    if detail.get("dynamicFare"):
        notes.append("dynamic pricing")
    if detail.get("mustBookAtAirport"):
        notes.append("aa.com says this must be booked at the airport")
    return ProductOffer(
        cabin=cabin,
        leg_cabins=tuple(leg_cabins),
        miles=int(miles),
        taxes=Decimal(str(tax["amount"])).quantize(Decimal("0.01")),
        currency=tax["currency"],
        seats_remaining=int(detail.get("seatsRemaining") or 0),
        notes=tuple(notes),
    )


def _flight_hints(raw: Any) -> frozenset[str]:
    """Best-effort flight codes from a slice that failed to parse. Empty means unknown."""
    codes: set[str] = set()
    try:
        for seg in raw.get("segments") or []:
            for leg in seg.get("legs") or []:
                flight = leg.get("flight") or seg.get("flight") or {}
                if flight.get("carrierCode") and flight.get("flightNumber"):
                    codes.add(f"{flight['carrierCode']}{normalize_number(flight['flightNumber'])}")
    except (AttributeError, TypeError):
        return frozenset()
    return frozenset(codes)


_OPERATED_BY = re.compile(r"operated by (.+?)(?: as | dba |$)", re.I)
_CARRIER_NAMES = {
    "japan airlines": "JL",
    "cathay pacific": "CX",
    "qatar airways": "QR",
    "british airways": "BA",
    "qantas": "QF",
    "finnair": "AY",
    "iberia": "IB",
    "malaysia airlines": "MH",
    "alaska airlines": "AS",
    "american eagle": "AA",
}


def _operating(leg: dict[str, Any], marketing: str) -> str | None:
    disclosure = (leg.get("operationalDisclosure") or "").strip()
    if not disclosure:
        return marketing
    match = _OPERATED_BY.search(disclosure)
    name = (match.group(1) if match else disclosure).lower()
    return next((code for key, code in _CARRIER_NAMES.items() if key in name), None)
