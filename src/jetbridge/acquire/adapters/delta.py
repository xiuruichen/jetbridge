"""Delta SkyMiles (delta.com).

Recon (2026-10-31):
* ``POST https://offer-api-prd.delta.com/prd/rm-offer-gql`` (GraphQL) with
  header ``authorization: GUEST`` returns award offers. Fetched in-page from a
  warmed delta.com tab (cross-site CORS request with the page's cookies).
* Two query shapes captured from the site (stored in ``gql/``):
  - flex calendar: ``calenderDateRequest`` +/-3 days -> 7 days per call,
    lowest fare per day for ``sortByBrandId`` (BE = any/lowest).
  - results: ``resultsPerRequestNum`` paging; each offer set = one itinerary,
    offers = brand columns with ``availableSeatCnt`` (exact, capped at 9) and
    per-leg ``brandByFlightLegs`` (mixed-cabin detection).
* Local timestamps have no offset -> interpreted in the airport's IANA zone.
"""

from __future__ import annotations

import json
import time
import uuid
from importlib import resources
from typing import Any

from patchright.async_api import Page

from jetbridge.acquire.adapters.base import (
    Adapter,
    Capabilities,
    flight_number,
    local_dt_airport,
    money,
)
from jetbridge.acquire.types import ParseResult, Query, RawResponse, SchemaDrift
from jetbridge.models.core import (
    AwardOffer,
    Cabin,
    Itinerary,
    Money,
    Provenance,
    SeatInfo,
    Segment,
)

ENDPOINT = "https://offer-api-prd.delta.com/prd/rm-offer-gql"

# Brand ids are prefixed B/C by fare type; strip to the base brand.
_BRAND_CABIN = {
    "MAIN": Cabin.ECONOMY,  # Main / Basic
    "DCP": Cabin.ECONOMY,  # Comfort (economy cabin, extra legroom)
    "DPPS": Cabin.PREMIUM,  # Premium Select
    "DPS": Cabin.PREMIUM,
    "PE": Cabin.PREMIUM,
    "D1": Cabin.BUSINESS,  # Delta One
    "FIRST": Cabin.FIRST,  # Domestic First
    "BU": Cabin.BUSINESS,  # partner business (seen on some partner itineraries)
}


def _brand_cabin(brand: str) -> Cabin:
    b = brand.upper()
    for cand in (b, b[1:]):
        if cand in _BRAND_CABIN:
            return _BRAND_CABIN[cand]
    raise ValueError(f"unknown Delta brand id {brand!r}")


def _query(name: str) -> str:
    return (
        resources.files("jetbridge.acquire.adapters")
        .joinpath("gql", name)
        .read_text(encoding="utf-8")
    )


def results_variables(q: Query, page_num: int = 1) -> dict[str, Any]:
    return {
        "offerSearchCriteria": {
            "productGroups": [{"productCategoryCode": "FLIGHTS"}],
            "offersCriteria": {
                "resultsPageNum": page_num,
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


_NO_OFFERS = "Unable to retrieve Flight offers"


def _no_offers_error(data: dict[str, Any]) -> bool:
    errs = data.get("errors") or []
    if not errs or (data.get("data") or {}).get("gqlSearchOffers"):
        return False
    return all(
        e.get("message") == "RetailOfferError"
        and _NO_OFFERS
        in str(((e.get("extensions") or {}).get("errors") or {}).get("developerMessage", ""))
        for e in errs
    )


def _pages(body: str) -> list[dict[str, Any]]:
    data = json.loads(body)
    return data if isinstance(data, list) else [data]


class DeltaAdapter(Adapter):
    id = "delta"
    site = "delta"
    program = "delta"
    warmup_url = "https://www.delta.com/"
    capabilities = Capabilities(award=True, cash=False, seat_counts="exact", calendar_days=7)
    price_independent_of_pax = True  # see Adapter docstring (verified)
    marker = '"gqlSearchOffers"'

    async def fetch(self, page: Page, q: Query) -> RawResponse:
        """Fetch every results page; body = JSON array of page responses."""
        tx = str(uuid.uuid4())
        pages: list[str] = []
        first: RawResponse | None = None
        page_num, total = 1, 1
        while page_num <= total and page_num <= 10:
            raw = await self._fetch(
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
                    "transactionid": f"{tx}_{int(time.time() * 1000)}",
                    "x-app-type": "dcom-shop",
                    "x-app-route": "SL-RSB",
                },
                body=json.dumps(
                    {
                        "query": _query("delta_results.graphql"),
                        "variables": results_variables(q, page_num),
                    }
                ),
            )
            first = first or raw
            if raw.status != 200:
                return raw
            pages.append(raw.body)
            try:
                props = json.loads(raw.body)["data"]["gqlSearchOffers"]["offerDataList"][
                    "responseProperties"
                ]
                total = int(props.get("pageResultCnt") or 1)
            except (KeyError, TypeError, ValueError):
                break
            page_num += 1
        assert first is not None
        first.body = "[" + ",".join(pages) + "]"
        first.meta["pages"] = len(pages)
        return first

    def is_empty_answer(self, raw: RawResponse) -> bool:
        try:
            pages = _pages(raw.body)
        except ValueError:
            return False
        if pages and all(_no_offers_error(d) for d in pages):
            return True
        rs = [(d.get("data") or {}).get("gqlSearchOffers") for d in pages]
        return bool(rs) and all(r is not None and not r.get("gqlOffersSets") for r in rs)

    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult:
        out = ParseResult()
        seen: set[str] = set()
        for data in _pages(raw.body):
            self._parse_page(data, q, prov, out, seen)
        return out

    def _parse_page(
        self, data: dict[str, Any], q: Query, prov: Provenance, out: ParseResult, seen: set[str]
    ) -> None:
        if data.get("errors"):
            msgs = "; ".join(str(e.get("message"))[:200] for e in data["errors"])
            if _no_offers_error(data):
                # Delta's explicit "no offers for this market" (seen 2026-10-31 for
                # CVG-CAN/CTS/FUK). Genuine empty; evidence kept as a warning.
                out.warnings.append(
                    "Delta: no offers for this market (Shopping API: Unable to retrieve Flight offers)"
                )
                return
            if not (data.get("data") or {}).get("gqlSearchOffers"):
                raise SchemaDrift(f"Delta GraphQL errors: {msgs}")
            out.warnings.append(f"Delta GraphQL partial errors: {msgs}")
        r = (data.get("data") or {}).get("gqlSearchOffers")
        if r is None:
            raise SchemaDrift("Delta response missing data.gqlSearchOffers")
        for oset in r.get("gqlOffersSets") or []:
            try:
                for offer in self._offers(oset, q, prov):
                    if offer.offer_id not in seen:
                        seen.add(offer.offer_id)
                        out.awards.append(offer)
            except (KeyError, TypeError, ValueError) as exc:
                out.warnings.append(f"skipped Delta offer set: {exc!r}")

    def _offers(self, oset: dict[str, Any], q: Query, prov: Provenance) -> list[AwardOffer]:
        legs: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for trip in oset["trips"]:
            for seg in trip["flightSegment"]:
                for leg in seg["flightLeg"]:
                    legs.append((seg, leg))
        offers: list[AwardOffer] = []
        for offer in oset.get("offers") or []:
            if offer.get("soldOut") or (offer.get("additionalOfferProperties") or {}).get(
                "soldOut"
            ):
                continue
            for item in offer.get("offerItems") or []:
                for ri in item.get("retailItems") or []:
                    for fi in (ri.get("retailItemMetaData") or {}).get("fareInformation") or []:
                        o = self._one(fi, legs, q, prov)
                        if o is not None:
                            offers.append(o)
        return offers

    def _one(
        self,
        fi: dict[str, Any],
        legs: list[tuple[dict[str, Any], dict[str, Any]]],
        q: Query,
        prov: Provenance,
    ) -> AwardOffer | None:
        prices = fi.get("farePrice") or []
        if not prices:
            return None
        tp = prices[0]["totalFarePrice"]
        miles = (tp.get("milesEquivalentPrice") or {}).get("mileCnt")
        if not miles:
            return None
        cash = tp.get("currencyEquivalentPrice") or {}
        tax_str = cash.get("formattedCurrencyAmt")
        if tax_str is None:
            raise ValueError("Delta fare has no formattedCurrencyAmt for taxes")
        brands = fi.get("brandByFlightLegs") or []
        if len(brands) != len(legs):
            raise ValueError(f"brand legs {len(brands)} != flight legs {len(legs)}")
        segments = []
        for (seg, leg), b in zip(legs, brands, strict=True):
            if not b.get("brandId"):
                return None
            mk = seg["marketingCarrier"]
            op = leg.get("operatingCarrier") or seg.get("operatingCarrier") or {}
            segments.append(
                Segment(
                    marketing_carrier=mk["carrierCode"],
                    flight_number=flight_number(mk["carrierNum"]),
                    operating_carrier=op.get("carrierCode"),
                    origin=leg["originAirportCode"],
                    destination=leg["destinationAirportCode"],
                    departs_at=local_dt_airport(
                        leg["scheduledDepartureLocalTs"], leg["originAirportCode"]
                    ),
                    arrives_at=local_dt_airport(
                        leg["scheduledArrivalLocalTs"], leg["destinationAirportCode"]
                    ),
                    cabin=_brand_cabin(b["brandId"]),
                    booking_class=(b.get("cosCode") or "")[:1] or None,
                    equipment=(leg.get("aircraft") or {}).get("fleetTypeCode"),
                )
            )
        cnt = fi.get("availableSeatCnt")
        if isinstance(cnt, int) and cnt > 0:
            seats = (
                SeatInfo(kind="lower_bound", count=max(9, q.pax))
                if cnt >= 9
                else SeatInfo(kind="exact", count=max(cnt, q.pax))
            )
        else:
            seats = SeatInfo(kind="lower_bound", count=q.pax)
        return AwardOffer(
            program="delta",
            itinerary=Itinerary(segments=tuple(segments)),
            miles=int(miles),
            taxes=Money(amount=money(tax_str), currency="USD"),
            seats=seats,
            provenance=prov,
            booking_url=(
                "https://www.delta.com/flight-search/search?action=findFlights&tripType=ONE_WAY"
                f"&originCity={q.origin}&destinationCity={q.destination}"
                f"&departureDate={q.date.strftime('%m/%d/%Y')}&paxCount={q.pax}&awardTravel=true"
            ),
        )
