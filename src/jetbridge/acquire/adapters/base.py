"""Adapter contract.

An adapter = (how to fetch) + (how to parse). Fetching goes through the
broker's warmed page; parsing is a *pure function* of the raw body so it can be
snapshot-tested against recorded fixtures and re-run on stored blobs.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import ClassVar, Literal

from patchright.async_api import Page

from jetbridge.acquire.broker import page_fetch
from jetbridge.acquire.types import ParseResult, Query, RawResponse
from jetbridge.kb import load_kb
from jetbridge.models.core import Provenance


@dataclass(frozen=True)
class Capabilities:
    award: bool
    cash: bool
    day_search: bool = True
    # calendar: days covered per call (0 = none); cabin_specific: calendar can be
    # filtered to a cabin; partner_complete: calendar is known to include partner
    # space. Pruning is only allowed when BOTH are True for the requested cabin.
    calendar_days: int = 0
    calendar_cabin_specific: bool = False
    calendar_partner_complete: bool = False
    seat_counts: Literal["exact", "bucket", "none"] = "none"
    max_pax: int = 9
    needs_login: bool = False


class Adapter(ABC):
    id: ClassVar[str]
    site: ClassVar[str]
    program: ClassVar[str | None]  # KB program id for award adapters
    warmup_url: ClassVar[str]
    capabilities: ClassVar[Capabilities]
    # A substring every genuine response contains (guards against a block page
    # being misread as "no availability").
    marker: ClassVar[str]
    # True only when it is verified that a fare seen in a k-pax search with an
    # EXACT seat count s is valid at the same per-pax price for any party <= s.
    # Verified on stored data 2026-10-31 (same flights+cabin, 1-pax vs 3-pax):
    #   delta 796/796 consistent, alaska 41/41  -> True
    #   united 653 underpriced despite >=9 bucket seats (dynamic by party size),
    #   american dynamic by party size           -> False
    price_independent_of_pax: ClassVar[bool] = False
    request_timeout_ms: ClassVar[int] = 90_000
    # True for adapters that NAVIGATE (tier T3): each concurrent fetch needs its own
    # tab. In-page fetch adapters (T1) share one tab and run concurrently in it.
    exclusive_tab: ClassVar[bool] = False

    @abstractmethod
    async def fetch(self, page: Page, q: Query) -> RawResponse: ...

    @abstractmethod
    def parse(self, raw: RawResponse, q: Query, prov: Provenance) -> ParseResult: ...

    def is_empty_answer(self, raw: RawResponse) -> bool:
        """Adapter-specific recognition of a *genuine* 'no flights' response."""
        return False

    # ---- helpers -----------------------------------------------------------
    async def _fetch(
        self,
        page: Page,
        url: str,
        *,
        method: str = "GET",
        headers: dict[str, str] | None = None,
        body: str | None = None,
        meta: dict | None = None,
    ) -> RawResponse:
        started = datetime.now(UTC)
        t0 = time.perf_counter()
        res = await page_fetch(
            page, url, method=method, headers=headers, body=body, timeout_ms=self.request_timeout_ms
        )
        return RawResponse(
            status=res["status"],
            body=res["text"] or "",
            tier="T1",
            started_at=started,
            latency_ms=int((time.perf_counter() - t0) * 1000),
            meta={"url": url, **(meta or {})},
        )


# ---- shared parsing utilities ---------------------------------------------


def money(value: str | float | int | Decimal) -> Decimal:
    if isinstance(value, str):
        value = value.replace(",", "").replace("$", "").strip()
    return Decimal(str(value)).quantize(Decimal("0.01"))


def local_dt_offset_hours(local: str, offset_hours: float, fmt: str) -> datetime:
    """'2027-05-28 11:20' + -5 -> aware datetime."""
    naive = datetime.strptime(local, fmt)
    return naive.replace(tzinfo=timezone(timedelta(hours=offset_hours)))


def local_dt_airport(local: str, airport: str) -> datetime:
    """Naive local ISO timestamp interpreted in the airport's IANA zone."""
    naive = datetime.fromisoformat(local)
    return naive.replace(tzinfo=load_kb().airport(airport).zone)


def flight_number(raw: str | int) -> str:
    s = str(raw).strip().lstrip("0") or "0"
    return s
