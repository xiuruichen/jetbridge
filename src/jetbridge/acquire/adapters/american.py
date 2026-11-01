"""American AAdvantage (aa.com).

Recon (2026-10-31):
* Guest award search works. The deep link ``/booking/search?...searchType=Award``
  auto-submits a form ``POST /booking/choose-flights/1`` with
  ``searchRequest=<json>``; the response is server-rendered HTML embedding the
  full result as Angular transfer state: ``<script id="ng-state">{...}</script>``.
* ``SearchData.itineraryResult.slices[]`` = itineraries; ``pricingDetail[]`` per
  product (COACH, PREMIUM_ECONOMY, BUSINESS, FIRST) with ``perPassengerAwardPoints``
  and ``perPassengerTaxesAndFees``; per-leg cabins in ``legs[].productDetails``.
* ``seatsRemaining`` is 0 when unknown (NOT sold out) -> seat count must come
  from pax probes.
* ``SearchData.weeklyResult.days`` = 13-day lowest-price strip (cabin-agnostic).

Tier T1 (primary): in-page fetch POST of that form (no rendering; concurrent in
one tab). Tier T3 (fallback): navigate a throwaway tab and capture the POST.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
import uuid
from datetime import UTC, datetime
from typing import Any

from patchright.async_api import Page

from jetbridge.acquire.adapters.base import Adapter, Capabilities, flight_number, money
from jetbridge.acquire.types import Blocked, ParseResult, Query, RawResponse, SchemaDrift
from jetbridge.models.core import (
    AwardOffer,
    Cabin,
    Itinerary,
    Money,
    Provenance,
    SeatInfo,
    Segment,
)

_PRODUCT_CABIN = {
    "COACH": Cabin.ECONOMY,
    "PREMIUM_ECONOMY": Cabin.PREMIUM,
    "BUSINESS": Cabin.BUSINESS,
    "FIRST": Cabin.FIRST,
}
_NG_STATE = re.compile(r'<script[^>]*id="ng-state"[^>]*>(.*?)</script>', re.S)


def search_url(q: Query) -> str:
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


def extract_state(html: str) -> dict[str, Any]:
    m = _NG_STATE.search(html)
    if not m:
        raise SchemaDrift("aa.com response has no ng-state transfer state")
    return json.loads(m.group(1))


class AmericanAdapter(Adapter):
    id = "american"
    site = "american"
    program = "american"
    warmup_url = "https://www.aa.com/homePage.do"
    capabilities = Capabilities(award=True, cash=False, seat_counts="none", calendar_days=13)
    marker = 'id="ng-state"'

    async def fetch(self, page: Page, q: Query) -> RawResponse:
        """T1: POST the same ``searchRequest`` form the site submits, via in-page fetch
        (no navigation/rendering; concurrent requests share the tab). Falls back to T3
        navigation in a throwaway tab if the response is not the expected page."""
        from jetbridge.acquire.detect import _CHALLENGE_RE

        raw = await self._fetch(
            page,
            f"/booking/choose-flights/1?sid={uuid.uuid4()}&locale=en_US",
            method="POST",
            headers={"content-type": "application/x-www-form-urlencoded", "accept": "text/html"},
            body="searchRequest="
            + urllib.parse.quote(json.dumps(search_request(q), separators=(",", ":"))),
        )
        if raw.status == 200 and self.marker in raw.body:
            return raw
        if raw.body and _CHALLENGE_RE.search(raw.body[:50_000]):
            return raw  # a block page: let the engine classify it (no point navigating)
        return await self._fetch_t3(page, q)

    async def _fetch_t3(self, page: Page, q: Query) -> RawResponse:
        from patchright.async_api import TimeoutError as PWTimeout

        from jetbridge.acquire.detect import _CHALLENGE_RE

        tab = await page.context.new_page()
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        try:
            try:
                async with tab.expect_response(
                    lambda r: "/booking/choose-flights/" in r.url and r.request.method == "POST",
                    timeout=45_000,
                ) as resp_info:
                    await tab.goto(
                        search_url(q), wait_until="commit", timeout=self.request_timeout_ms
                    )
                resp = await resp_info.value
            except PWTimeout as exc:
                # aa.com can interpose its human-verification page BEFORE the search
                # form posts; then no search response arrives. That is a block.
                try:
                    html = await tab.content()
                except Exception:
                    html = ""
                if _CHALLENGE_RE.search(html[:50_000]):
                    raise Blocked("aa.com human-verification page shown before search") from exc
                raise
            body = await resp.text()
            status = resp.status
        finally:
            await tab.close()
        return RawResponse(
            status=status,
            body=body,
            tier="T3",
            started_at=started,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            meta={"fallback": "t3"},
        )

    def is_empty_answer(self, raw: RawResponse) -> bool:
        try:
            st = extract_state(raw.body)
        except (SchemaDrift, ValueError):
            return False
        ir = st.get("SearchData", {}).get("itineraryResult") or {}
        # Only a clean, error-free result with zero slices counts as "no flights".
        # aa.com error codes (e.g. "309") have unverified meaning -> treated as failures.
        return (
            not ir.get("error") and not ir.get("slices") and ir.get("responseMetadata") is not None
        )

    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult:
        out = ParseResult()
        st = extract_state(raw.body)
        sd = st.get("SearchData")
        if not isinstance(sd, dict) or "itineraryResult" not in sd:
            raise SchemaDrift("ng-state missing SearchData.itineraryResult")
        ir = sd["itineraryResult"]
        if ir.get("error"):
            out.warnings.append(f"aa.com itineraryResult.error={ir['error']!r}")
        if (ir.get("responseMetadata") or {}).get("cached"):
            out.warnings.append("aa.com marked this result as cached")
        seen: set[str] = set()
        for sl in ir.get("slices") or []:
            try:
                for offer in self._offers(sl, q, prov):
                    if offer.offer_id not in seen:
                        seen.add(offer.offer_id)
                        out.awards.append(offer)
            except (KeyError, TypeError, ValueError) as exc:
                out.warnings.append(f"skipped slice {sl.get('id')}: {exc!r}")
        return out

    def _offers(self, sl: dict[str, Any], q: Query, prov: Provenance) -> list[AwardOffer]:
        legs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for seg in sl["segments"]:
            for leg in seg["legs"]:
                legs.append((seg, leg))
        offers = []
        for pd in sl.get("pricingDetail") or []:
            ptype = pd.get("productType")
            if ptype not in _PRODUCT_CABIN or not pd.get("productAvailable"):
                continue
            miles = pd.get("perPassengerAwardPoints")
            if not miles:
                continue
            segments = []
            for seg, leg in legs:
                prod = next(
                    (p for p in leg.get("productDetails") or [] if p.get("productType") == ptype),
                    None,
                )
                if prod is None or prod.get("cabinType") not in _PRODUCT_CABIN:
                    raise ValueError(
                        f"no cabin for {ptype} on leg {leg.get('origin', {}).get('code')}"
                    )
                fl = leg.get("flight") or seg["flight"]
                segments.append(
                    Segment(
                        marketing_carrier=fl["carrierCode"],
                        flight_number=flight_number(fl["flightNumber"]),
                        operating_carrier=_operating(leg, fl["carrierCode"]),
                        origin=leg["origin"]["code"],
                        destination=leg["destination"]["code"],
                        departs_at=datetime.fromisoformat(leg["departureDateTime"]),
                        arrives_at=datetime.fromisoformat(leg["arrivalDateTime"]),
                        cabin=_PRODUCT_CABIN[prod["cabinType"]],
                        booking_class=(prod.get("bookingCode") or None),
                        equipment=leg.get("aircraftCode"),
                    )
                )
            tax = pd["perPassengerTaxesAndFees"]
            remaining = int(pd.get("seatsRemaining") or 0)
            seats = (
                SeatInfo(kind="exact", count=max(remaining, q.pax))
                if remaining > 0
                else SeatInfo(kind="lower_bound", count=q.pax)
            )
            notes = []
            if pd.get("dynamicFare"):
                notes.append("dynamic pricing")
            if pd.get("mustBookAtAirport"):
                notes.append("must book at airport")
            offers.append(
                AwardOffer(
                    program="american",
                    itinerary=Itinerary(segments=tuple(segments)),
                    miles=int(miles),
                    taxes=Money(amount=money(tax["amount"]), currency=tax["currency"]),
                    seats=seats,
                    provenance=prov,
                    booking_url=search_url(q),
                    notes=tuple(notes),
                )
            )
        return offers


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
    "envoy": "AA",
    "piedmont": "AA",
    "psa airlines": "AA",
    "republic": "AA",
    "skywest": "AA",
    "mesa": "AA",
}


def _operating(leg: dict[str, Any], marketing: str) -> str | None:
    disc = (leg.get("operationalDisclosure") or "").strip()
    if not disc:
        return marketing
    m = _OPERATED_BY.search(disc)
    name = (m.group(1) if m else disc).lower()
    for k, code in _CARRIER_NAMES.items():
        if k in name:
            return code
    return None  # unknown operator: leave unset rather than guess
