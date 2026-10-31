"""Alaska/Hawaiian Atmos Rewards (alaskaair.com).

Recon (2026-10-31):
* Guest award search: ``GET /search/results?...&ShoppingMethod=onlineaward&awardBooking=true``
  returns server-rendered SvelteKit HTML. Results are a deferred JS object
  literal: ``__sveltekit_<id>.resolve(1, () => [{departureStation, rows:[...]}])``.
* ``rows[].solutions`` = fare columns (e.g. REFUNDABLE_BUSINESS / REFUNDABLE_MAIN)
  with ``atmosPoints``, ``grandTotal``, exact ``seatsRemaining`` and per-segment
  cabins via ``fareSegmentMapping[].segmentHash``.
* ``POST /search/api/shoulderDates`` gives a cabin-agnostic lowest-points strip.

Fetch (T1): in-page GET of the results HTML with the session's cookies; the data
literal is then evaluated to JSON inside a blank about:blank tab (no site code
runs there, only the data literal). The raw body we store and parse is that JSON.
"""

from __future__ import annotations

import json
import re
import urllib.parse
from typing import Any

from patchright.async_api import Page

from jetbridge.acquire.adapters.base import Adapter, Capabilities, flight_number, money
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

_DATA_RE = re.compile(r"__sveltekit_\w+\.resolve\(1, \(\) => \[([\s\S]*?)\]\)</script>")
_CABIN = {
    "COACH": Cabin.ECONOMY,
    "MAIN": Cabin.ECONOMY,
    "PREMIUM": Cabin.PREMIUM,
    "PREMIUM_ECONOMY": Cabin.PREMIUM,
    "PREMIUMCLASS": Cabin.PREMIUM,
    "BUSINESS": Cabin.BUSINESS,
    "FIRST": Cabin.FIRST,
}
_OPERATORS = [
    (re.compile(r"as hawaiian", re.I), "HA"),
    (re.compile(r"horizon", re.I), "AS"),
    (re.compile(r"skywest", re.I), "AS"),
    (re.compile(r"japan airlines", re.I), "JL"),
    (re.compile(r"cathay", re.I), "CX"),
    (re.compile(r"starlux", re.I), "JX"),
    (re.compile(r"qatar", re.I), "QR"),
    (re.compile(r"british airways", re.I), "BA"),
    (re.compile(r"american airlines", re.I), "AA"),
    (re.compile(r"korean air", re.I), "KE"),
    (re.compile(r"singapore", re.I), "SQ"),
    (re.compile(r"emirates", re.I), "EK"),
    (re.compile(r"qantas", re.I), "QF"),
    (re.compile(r"finnair", re.I), "AY"),
    (re.compile(r"malaysia", re.I), "MH"),
    (re.compile(r"fiji", re.I), "FJ"),
]


def search_url(q: Query) -> str:
    params = {
        "A": str(q.pax),
        "O": q.origin,
        "D": q.destination,
        "OD": q.date.isoformat(),
        "OT": "Anytime",
        "RT": "false",
        "UPG": "none",
        "ShoppingMethod": "onlineaward",
        "awardBooking": "true",
    }
    return "https://www.alaskaair.com/search/results?" + urllib.parse.urlencode(params)


_EVAL_JS = "(src) => JSON.stringify((new Function('return [' + src + ']'))()[0])"


class AlaskaAdapter(Adapter):
    id = "alaska"
    site = "alaska"
    program = "alaska"
    warmup_url = "https://www.alaskaair.com/"
    capabilities = Capabilities(award=True, cash=False, seat_counts="exact")
    marker = '"rows"'
    # verified 2026-10-31: SEA-HND 1 vs 3 pax identical per-pax points/taxes
    price_independent_of_pax = True

    async def fetch(self, page: Page, q: Query) -> RawResponse:
        raw = await self._fetch(page, search_url(q), headers={"accept": "text/html"})
        if raw.status != 200:
            return raw
        m = _DATA_RE.search(raw.body)
        if m is None:
            # leave the HTML in place so block detection / drift reporting can see it
            raw.meta["note"] = "results data literal not found"
            return raw
        blank = await page.context.new_page()
        try:
            raw.body = await blank.evaluate(_EVAL_JS, m.group(1))
        finally:
            await blank.close()
        raw.meta["derived"] = "json from sveltekit resolve(1) literal"
        return raw

    def is_empty_answer(self, raw: RawResponse) -> bool:
        try:
            d = json.loads(raw.body)
        except ValueError:
            return False
        return isinstance(d, dict) and d.get("rows") == [] and "departureStation" in d

    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult:
        out = ParseResult()
        try:
            d = json.loads(raw.body)
        except ValueError as exc:
            raise SchemaDrift("Alaska body is not the derived results JSON") from exc
        if "rows" not in d:
            raise SchemaDrift("Alaska results missing 'rows'")
        seen: set[str] = set()
        for row in d["rows"]:
            try:
                for o in self._offers(row, q, prov):
                    if o.offer_id not in seen:
                        seen.add(o.offer_id)
                        out.awards.append(o)
            except (KeyError, TypeError, ValueError) as exc:
                out.warnings.append(f"skipped Alaska row {row.get('id')}: {exc!r}")
        return out

    def _offers(self, row: dict[str, Any], q: Query, prov: Provenance) -> list[AwardOffer]:
        segs = row["segments"]
        offers = []
        for col, sol in (row.get("solutions") or {}).items():
            if not sol or not sol.get("atmosPoints"):
                continue
            by_hash = {m["segmentHash"]: m for m in sol.get("fareSegmentMapping") or []}
            segments = []
            for i, s in enumerate(segs):
                mp = by_hash.get(s["hash"])
                cab = mp["cabin"] if mp else (sol.get("cabins") or [None] * len(segs))[i]
                if cab not in _CABIN:
                    raise ValueError(f"unknown Alaska cabin {cab!r} in {col}")
                car = s["displayCarrier"]
                segments.append(
                    Segment(
                        marketing_carrier=car["carrierCode"],
                        flight_number=flight_number(car["flightNumber"]),
                        operating_carrier=_operator(
                            s.get("operationalDisclosure"), car["carrierCode"]
                        ),
                        origin=s["departureStation"],
                        destination=s["arrivalStation"],
                        departs_at=_dt(s["departureTime"]),
                        arrives_at=_dt(s["arrivalTime"]),
                        cabin=_CABIN[cab],
                        booking_class=(mp or {}).get("bookingCode") or None,
                        equipment=s.get("aircraftCode"),
                    )
                )
            per, allp = int(sol["atmosPoints"]), sol.get("allPaxPoints")
            if allp is not None and int(allp) != per * q.pax:
                raise ValueError(
                    f"allPaxPoints {allp} != atmosPoints {per} x pax {q.pax}: per-pax semantics changed"
                )
            n = int(sol.get("seatsRemaining") or 0)
            if n <= 0:
                seats = SeatInfo(kind="lower_bound", count=q.pax)
            elif n >= 9:
                seats = SeatInfo(kind="lower_bound", count=max(9, q.pax))
            else:
                seats = SeatInfo(kind="exact", count=max(n, q.pax))
            notes = [f"Alaska fare column {col}"]
            if sol.get("isDiscounted"):
                notes.append("discounted award")
            offers.append(
                AwardOffer(
                    program="alaska",
                    itinerary=Itinerary(segments=tuple(segments)),
                    miles=int(sol["atmosPoints"]),
                    taxes=Money(amount=money(sol["grandTotal"]), currency="USD"),
                    seats=seats,
                    provenance=prov,
                    booking_url=search_url(q),
                    notes=tuple(notes),
                )
            )
        return offers


def _dt(s: str):
    from datetime import datetime

    return datetime.fromisoformat(s)


def _operator(disclosure: str | None, marketing: str) -> str | None:
    if not disclosure:
        return marketing
    for rx, code in _OPERATORS:
        if rx.search(disclosure):
            return code
    return None
