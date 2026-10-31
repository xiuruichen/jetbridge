"""United MileagePlus (united.com).

Recon (2026-10-31):
* The UI shows a sign-in modal on award results, but the internal endpoint
  ``POST /api/flight/FetchSSENestedFlights`` returns award pricing for a guest
  bearer token from ``GET /api/auth/anonymous-token`` (in-page fetch, tier T1).
  Guest access chosen by the project owner (2026-10-31).
* Response is Server-Sent Events; ``flightOption`` events hold itineraries.
* Each segment carries ``bookingClassAvailability`` ("J9|JN9|I0|..."), so the
  award bucket count for the booked class gives real seat counts (capped at 9).
* Top-level product ``cabinType`` describes the FIRST segment; connecting
  segments' cabins are in ``connections[i].products`` with the same productType.
"""

from __future__ import annotations

import json
from typing import Any

from patchright.async_api import Page

from jetbridge.acquire.adapters.base import (
    Adapter,
    Capabilities,
    flight_number,
    local_dt_offset_hours,
    money,
)
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
    "MIN-ECONOMY-SURP-OR-DISP": Cabin.ECONOMY,
    "ECO-PREMIUM-DISP": Cabin.PREMIUM,
    "MIN-BUSINESS-SURP-OR-DISP": Cabin.BUSINESS,
    "MIN-BUSINESS-SURP-OR-DISP-NOT-MIXED": Cabin.BUSINESS,
    "MIN-FIRST-SURP-OR-DISP": Cabin.FIRST,
    "MIN-FIRST-SURP-OR-DISP-NOT-MIXED": Cabin.FIRST,
}
_SEG_CABIN = {
    "coach": Cabin.ECONOMY,
    "economy": Cabin.ECONOMY,
    "premiumplus": Cabin.PREMIUM,
    "premium economy": Cabin.PREMIUM,
    "business": Cabin.BUSINESS,
    "first": Cabin.FIRST,
}
_TIME_FMT = "%Y-%m-%d %H:%M"


def _body(q: Query) -> dict[str, Any]:
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


class UnitedAdapter(Adapter):
    id = "united"
    site = "united"
    program = "united"
    warmup_url = "https://www.united.com/en/us"
    capabilities = Capabilities(award=True, cash=False, seat_counts="bucket")
    marker = '"type":"meta"'
    price_independent_of_pax = True

    async def fetch(self, page: Page, q: Query) -> RawResponse:
        tok = await self._fetch(page, "/api/auth/anonymous-token")
        if tok.status != 200:
            raise Blocked(f"anonymous-token HTTP {tok.status}")
        try:
            token = json.loads(tok.body)["data"]["token"]["hash"]
        except (KeyError, ValueError) as exc:
            raise SchemaDrift(f"anonymous-token shape changed: {tok.body[:200]}") from exc
        return await self._fetch(
            page,
            "/api/flight/FetchSSENestedFlights",
            method="POST",
            headers={
                "content-type": "application/json",
                "accept": "text/event-stream",
                "x-authorization-api": f"bearer {token}",
            },
            body=json.dumps(_body(q)),
        )

    def is_empty_answer(self, raw: RawResponse) -> bool:
        return self.marker in raw.body and '"type":"flightOption"' not in raw.body

    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult:
        out = ParseResult()
        events = []
        for line in raw.body.splitlines():
            if line.startswith("data: "):
                try:
                    events.append(json.loads(line[6:]))
                except ValueError:
                    out.warnings.append(f"undecodable SSE line: {line[:120]}")
        meta = next((e for e in events if e.get("type") == "meta"), None)
        if meta is None:
            raise SchemaDrift("no meta event in United SSE stream")
        if meta.get("status") not in (None, "success"):
            out.warnings.append(f"United meta status={meta.get('status')}")
        seen: set[str] = set()
        for ev in events:
            if ev.get("type") != "flightOption":
                continue
            try:
                for offer in self._offers(ev["flight"], q, prov):
                    if offer.offer_id not in seen:
                        seen.add(offer.offer_id)
                        out.awards.append(offer)
            except (KeyError, TypeError, ValueError) as exc:
                out.warnings.append(f"skipped flightOption {ev.get('seq')}: {exc!r}")
        return out

    def _offers(self, f: dict[str, Any], q: Query, prov: Provenance) -> list[AwardOffer]:
        segs_raw = [f, *f.get("connections", [])]
        offers: list[AwardOffer] = []
        for p_idx, prod in enumerate(f["products"]):
            ptype = prod.get("productType")
            if ptype not in _PRODUCT_CABIN or not prod.get("prices"):
                continue
            miles = next(
                (int(x["amount"]) for x in prod["prices"] if x.get("currency") == "MILES"), None
            )
            tax = next((x for x in prod["prices"] if x.get("pricingType") == "Tax"), None)
            if not miles or tax is None:
                continue
            seg_cabins: list[Cabin] = []
            seg_codes: list[str] = []
            for i, s in enumerate(segs_raw):
                sp = prod if i == 0 else _match_product(s, ptype, p_idx)
                if sp is None or not sp.get("cabinType"):
                    raise ValueError(f"no cabin for segment {i} product {ptype}")
                seg_cabins.append(_SEG_CABIN[sp["cabinType"].lower()])
                seg_codes.append(sp.get("bookingCode") or "")
            segments = tuple(
                Segment(
                    marketing_carrier=s["marketingCarrier"],
                    flight_number=flight_number(s["flightNumber"]),
                    operating_carrier=s.get("operatingCarrier") or None,
                    origin=s["origin"],
                    destination=s["destination"],
                    departs_at=local_dt_offset_hours(
                        s["departDateTime"], s["orgTimezoneOffset"], _TIME_FMT
                    ),
                    arrives_at=local_dt_offset_hours(
                        s["destinationDateTime"], s["destTimezoneOffset"], _TIME_FMT
                    ),
                    cabin=c,
                    booking_class=code[0] if code else None,
                    equipment=(s.get("equipmentDisclosures") or {}).get("equipmentType"),
                )
                for s, c, code in zip(segs_raw, seg_cabins, seg_codes, strict=True)
            )
            itin = Itinerary(segments=segments)
            seats = _seats(segs_raw, seg_codes, q.pax)
            notes = [f"United {prod.get('awardType', '?')} award"]
            if ptype.endswith("NOT-MIXED"):
                notes.append("no mixed cabin")
            offers.append(
                AwardOffer(
                    program="united",
                    itinerary=itin,
                    miles=miles,
                    taxes=Money(amount=money(tax["amount"]), currency=tax.get("currency", "USD")),
                    seats=seats,
                    provenance=prov,
                    booking_url=_deeplink(q),
                    notes=tuple(notes),
                )
            )
        return offers


def _match_product(seg: dict[str, Any], ptype: str, idx: int) -> dict[str, Any] | None:
    prods = seg.get("products") or []
    if idx < len(prods) and prods[idx].get("productType") == ptype:
        return prods[idx]
    return next((p for p in prods if p.get("productType") == ptype), None)


def _seats(segs: list[dict[str, Any]], codes: list[str], pax: int) -> SeatInfo:
    counts: list[int] = []
    for s, code in zip(segs, codes, strict=True):
        avail = {}
        for tok in (s.get("bookingClassAvailability") or "").split("|"):
            if len(tok) >= 2 and tok[-1].isdigit():
                avail[tok.rstrip("0123456789")] = int(tok[len(tok.rstrip("0123456789")) :])
        if code in avail:
            counts.append(avail[code])
        else:
            # Bucket not published for this segment: fall back to what the
            # query proves (a pax=N search succeeded).
            return SeatInfo(kind="lower_bound", count=pax)
    n = min(counts) if counts else 0
    if n <= 0:
        return SeatInfo(kind="lower_bound", count=pax)
    if n >= 9:
        return SeatInfo(kind="lower_bound", count=max(9, pax))
    return SeatInfo(kind="exact", count=max(n, pax))


def _deeplink(q: Query) -> str:
    return (
        "https://www.united.com/en/us/fsr/choose-flights?"
        f"f={q.origin}&t={q.destination}&d={q.date.isoformat()}&tt=1&at=1&sc=7&px={q.pax}"
        "&taxng=1&newHP=True&clm=7&st=bestmatches&tqp=A"
    )
