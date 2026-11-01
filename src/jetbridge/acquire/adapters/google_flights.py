"""Google Flights cash fares (google.com/travel/flights).

Recon (2026-10-31):
* Search URL: ``/travel/flights/search?tfs=<protobuf>`` (encoder: ``gflights_tfs``).
* Results arrive via XHR ``.../FlightsFrontendService/GetShoppingResults`` in
  Google's batchexecute format: ``)]}'`` + length-prefixed JSON chunks of
  ``["wrb.fr", null, "<json>"]``. Inner payload ``[2][0]`` = "best" flights,
  ``[3][0]`` = other flights. Each item ``[itinerary, [[null, price], token], ...]``.
* Itinerary ``[2]`` = legs; leg: [3] origin, [6] destination, [8]/[10] dep/arr
  time ``[h, m]`` (minutes omitted when 0), [20]/[21] dep/arr date ``[y, m, d]``,
  [22] ``[carrier, number, _, name]``, [2] operator text, [17] aircraft name.
* Cabin per leg is not exposed; legs are recorded in the searched cabin and the
  offer carries a note saying so.

Fetch (T3): navigate a warmed google.com tab to the tfs URL and collect every
GetShoppingResults response for 6 s after the first (results stream in).
Body stored/parsed = JSON list of the raw batchexecute bodies.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from patchright.async_api import Page, Response

from jetbridge.acquire.adapters.base import Adapter, Capabilities, flight_number
from jetbridge.acquire.adapters.gflights_tfs import TFS, Leg
from jetbridge.acquire.types import ParseResult, Query, RawResponse, SchemaDrift
from jetbridge.kb import load_kb
from jetbridge.models.core import Cabin, CashOffer, Itinerary, Money, Provenance, Segment

_LEN = re.compile(r"\s*\d+\s*")
QUIET_S = 1.5
MAX_TAIL_S = 6.0


def batchexecute_payloads(body: str) -> list[Any]:
    """Decode a batchexecute response body into its inner JSON payloads."""
    dec = json.JSONDecoder()
    i = body.find("\n")
    if not body.startswith(")]}'") or i < 0:
        raise SchemaDrift("not a batchexecute body")
    out = []
    i += 1
    while i < len(body):
        m = _LEN.match(body, i)
        if not m or m.end() >= len(body):
            break
        try:
            arr, i = dec.raw_decode(body, m.end())
        except ValueError:
            break
        for item in arr:
            if isinstance(item, list) and len(item) > 2 and item[0] == "wrb.fr" and item[2]:
                out.append(json.loads(item[2]))
    return out


class GoogleFlightsAdapter(Adapter):
    id = "google_flights"
    site = "google"
    program = None
    warmup_url = "https://www.google.com/travel/flights?hl=en&gl=us&curr=USD"
    capabilities = Capabilities(award=False, cash=True, seat_counts="none")
    marker = "wrb.fr"
    exclusive_tab = True

    def url(self, q: Query) -> str:
        return TFS(
            (Leg(q.date.isoformat(), q.origin, q.destination),),
            adults=q.pax,
            cabin=q.cabin or Cabin.ECONOMY,
        ).url()

    async def fetch(self, page: Page, q: Query) -> RawResponse:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        bodies: list[str] = []
        statuses: list[int] = []
        first = asyncio.Event()
        tasks: list[asyncio.Task] = []

        async def grab(r: Response) -> None:
            if "GetShoppingResults" not in r.url:
                return
            statuses.append(r.status)
            try:
                bodies.append(await r.text())
            except Exception:
                return
            first.set()

        def on_resp(r: Response) -> None:
            tasks.append(asyncio.ensure_future(grab(r)))

        page.on("response", on_resp)
        try:
            await page.goto(
                self.url(q), wait_until="domcontentloaded", timeout=self.request_timeout_ms
            )
            try:
                await asyncio.wait_for(first.wait(), timeout=45)
            except TimeoutError:
                pass
            # Results stream in several responses; stop once the stream has been
            # quiet for QUIET_S (bounded by MAX_TAIL_S) instead of a fixed sleep.
            tail_start = time.perf_counter()
            seen = len(bodies)
            last_change = time.perf_counter()
            while bodies and time.perf_counter() - tail_start < MAX_TAIL_S:
                await asyncio.sleep(0.25)
                if len(bodies) != seen:
                    seen, last_change = len(bodies), time.perf_counter()
                elif time.perf_counter() - last_change >= QUIET_S:
                    break
            if tasks:
                await asyncio.wait(tasks, timeout=5)
        finally:
            page.remove_listener("response", on_resp)
        status = 200 if bodies else (statuses[0] if statuses else None)
        return RawResponse(
            status=status,
            body=json.dumps(bodies) if bodies else (await page.content())[:200_000],
            tier="T3",
            started_at=started,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            meta={"url": self.url(q), "responses": len(bodies)},
        )

    def is_empty_answer(self, raw: RawResponse) -> bool:
        try:
            bodies = json.loads(raw.body)
            payloads = [p for b in bodies for p in batchexecute_payloads(b)]
        except (ValueError, SchemaDrift):
            return False
        return bool(payloads) and not any(_items(p) for p in payloads)

    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult:
        out = ParseResult()
        try:
            bodies = json.loads(raw.body)
        except ValueError as exc:
            raise SchemaDrift("Google Flights body is not the JSON list of responses") from exc
        cabin = q.cabin or Cabin.ECONOMY
        seen: dict[str, CashOffer] = {}
        for b in bodies:
            for payload in batchexecute_payloads(b):
                for item in _items(payload):
                    try:
                        o = _offer(item, q, cabin, prov)
                    except (IndexError, KeyError, TypeError, ValueError) as exc:
                        out.warnings.append(f"skipped Google Flights item: {exc!r}")
                        continue
                    if o is None:
                        continue
                    # later responses refine earlier ones: keep the newest per flights
                    seen[o.itinerary.flight_key] = o
        out.cash = list(seen.values())
        return out


def _items(payload: Any) -> list[Any]:
    out = []
    for idx in (2, 3):
        try:
            block = payload[idx]
        except (IndexError, TypeError):
            continue
        if isinstance(block, list) and block and isinstance(block[0], list):
            out.extend(block[0])
    return out


def _t(hm: list | None) -> tuple[int, int]:
    hm = hm or [0]
    return int(hm[0] or 0), int(hm[1] if len(hm) > 1 and hm[1] is not None else 0)


def _offer(item: list, q: Query, cabin: Cabin, prov: Provenance) -> CashOffer | None:
    it, price_block = item[0], item[1]
    if not price_block or not price_block[0] or price_block[0][1] is None:
        return None  # no price shown (e.g. unavailable)
    total = Decimal(str(price_block[0][1]))
    kb = load_kb()
    segs = []
    for leg in it[2]:
        o, d = leg[3], leg[6]
        dy, dm, dd = leg[20]
        ay, am, ad = leg[21]
        dh, dmin = _t(leg[8])
        ah, amin = _t(leg[10])
        car = leg[22]
        segs.append(
            Segment(
                marketing_carrier=car[0],
                flight_number=flight_number(car[1]),
                operating_carrier=None if leg[2] else car[0],
                origin=o,
                destination=d,
                departs_at=datetime(dy, dm, dd, dh, dmin, tzinfo=kb.airport(o).zone),
                arrives_at=datetime(ay, am, ad, ah, amin, tzinfo=kb.airport(d).zone),
                cabin=cabin,
                equipment=leg[17] if len(leg) > 17 else None,
            )
        )
    per_pax = (total / q.pax).quantize(Decimal("0.01")) if PRICE_IS_TOTAL else total
    return CashOffer(
        source="google_flights",
        itinerary=Itinerary(segments=tuple(segs)),
        price=Money(amount=per_pax, currency="USD"),
        seats=None,
        provenance=prov,
        booking_url=TFS((Leg(q.date.isoformat(), q.origin, q.destination),), q.pax, cabin).url(),
    )


# Google's displayed price is for ALL passengers. Verified live 2026-10-31:
# CVG-ICN J, AC8704/AC61: pax=1 $4,827; pax=3 $14,480 (= $4,826.67/pax).
PRICE_IS_TOTAL = True
