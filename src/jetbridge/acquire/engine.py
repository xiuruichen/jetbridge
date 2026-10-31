"""Acquisition engine: executes Queries with pacing, retries, block handling,
provenance and persistence.

Outcome semantics (what the coverage ledger records):
  ok       genuine response with >=1 offer
  empty    genuine response, positively recognized as "no flights"
  blocked  bot-manager challenge / 403 (after retries + session rotation)
  failed   network / HTTP / schema-drift error (after retries)
A response is never treated as "empty" unless the adapter positively
recognizes it as a genuine no-availability answer.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.broker import SessionBroker
from jetbridge.acquire.cache import cached, record_fetch
from jetbridge.acquire.detect import classify
from jetbridge.acquire.scheduler import BreakerOpen, BudgetExhausted, Scheduler, SitePolicy
from jetbridge.acquire.types import AdapterError, Outcome, ParseResult, Query, RawResponse
from jetbridge.models.core import AwardOffer, CashOffer, Provenance
from jetbridge.store.db import ObservationRow, session

log = logging.getLogger("jetbridge.engine")


@dataclass
class QueryResult:
    query: Query
    status: str  # ok|empty|blocked|failed|skipped
    fetch_id: str | None = None
    parsed: ParseResult = field(default_factory=ParseResult)
    reason: str | None = None
    attempts: int = 0
    from_cache: bool = False


QUARANTINE_AFTER = 2

DEFAULT_POLICIES = {
    "united": SitePolicy(min_interval_s=4, max_interval_s=9),
    "american": SitePolicy(min_interval_s=8, max_interval_s=16, cooldown_s=600),
    "delta": SitePolicy(min_interval_s=3, max_interval_s=7),
    "alaska": SitePolicy(min_interval_s=4, max_interval_s=8),
    "google": SitePolicy(min_interval_s=6, max_interval_s=12),
}


class Engine:
    def __init__(
        self,
        adapters: dict[str, Adapter],
        broker: SessionBroker | None,
        scheduler: Scheduler | None = None,
        *,
        cache_ttl: timedelta = timedelta(minutes=45),
        max_attempts: int = 3,
        search_id: str | None = None,
        logins_enabled: frozenset[str] = frozenset(),
    ):
        self.adapters = adapters
        self.broker = broker
        self.scheduler = scheduler or Scheduler(DEFAULT_POLICIES)
        self.cache_ttl = cache_ttl
        self.max_attempts = max_attempts
        self.search_id = search_id
        self.logins_enabled = logins_enabled
        # Site quarantine: after QUARANTINE_AFTER consecutive *queries* end blocked
        # (each already retried), stop spending time on that site for this run when
        # no human can clear the challenge. Remaining queries are recorded as
        # blocked with a remedy, and `resume --show-browser` retries them.
        self._block_streak: dict[str, int] = {}
        self.quarantined: dict[str, str] = {}

    async def run(self, q: Query, *, use_cache: bool = True) -> QueryResult:
        adapter = self.adapters[q.adapter]
        if adapter.site in self.quarantined:
            return QueryResult(q, "blocked", reason=self.quarantined[adapter.site])
        if adapter.capabilities.needs_login and adapter.id not in self.logins_enabled:
            return QueryResult(
                q,
                "skipped",
                reason=f"{adapter.id} requires sign-in; not enabled in profile (logins_enabled)",
            )
        if use_cache and self.cache_ttl > timedelta(0):
            hit = cached(q, self.cache_ttl)
            if hit is not None:
                fid, raw = hit
                res = self._interpret(adapter, q, raw, fid)
                res.from_cache = True
                return res

        last_reason = "not attempted"
        last_outcome = Outcome.ERROR
        for attempt in range(1, self.max_attempts + 1):
            if self.broker is None:
                return QueryResult(q, "failed", reason="no browser broker (offline mode)")
            try:
                async with self.scheduler.slot(adapter.site):
                    page = await self.broker.page(adapter.site, adapter.warmup_url)
                    raw = await adapter.fetch(page, q)
            except BreakerOpen as exc:
                if not self.scheduler._fast:
                    await asyncio.sleep(min(exc.retry_in, 240))
                last_reason, last_outcome = str(exc), Outcome.BLOCKED
                continue
            except BudgetExhausted as exc:
                return QueryResult(q, "failed", reason=str(exc), attempts=attempt)
            except AdapterError as exc:
                last_reason, last_outcome = f"{type(exc).__name__}: {exc}", exc.outcome
                record_fetch(q, None, exc.outcome, last_reason)
                await self._after_failure(adapter, exc.outcome, attempt)
                continue
            except Exception as exc:  # browser crash, navigation timeout, ...
                last_reason, last_outcome = f"{type(exc).__name__}: {exc}"[:500], Outcome.ERROR
                record_fetch(q, None, Outcome.ERROR, last_reason)
                await self._after_failure(adapter, Outcome.ERROR, attempt)
                continue

            outcome, detail = classify(raw, require_marker=adapter.marker)
            if outcome is Outcome.OK:
                fid = record_fetch(q, raw, Outcome.OK)
                self.scheduler.report(adapter.site, blocked=False)
                self._block_streak[adapter.site] = 0
                res = self._interpret(adapter, q, raw, fid)
                res.attempts = attempt
                return res
            last_reason, last_outcome = detail or outcome.value, outcome
            record_fetch(q, raw, outcome, detail)
            await self._after_failure(adapter, outcome, attempt)

        status = "blocked" if last_outcome in (Outcome.BLOCKED, Outcome.RATE_LIMITED) else "failed"
        if status == "blocked":
            n = self._block_streak.get(adapter.site, 0) + 1
            self._block_streak[adapter.site] = n
            visible = self.broker is not None and not self.broker.offscreen
            if n >= QUARANTINE_AFTER and not visible:
                self.quarantined[adapter.site] = (
                    f"{adapter.site} keeps showing human verification; skipped for this run. "
                    "Run `jetbridge resume <search_id> --show-browser` and complete the check in the "
                    "Chrome window to retry these queries."
                )
                log.warning("quarantined %s after %d blocked queries", adapter.site, n)
        return QueryResult(q, status, reason=last_reason, attempts=self.max_attempts)

    async def _after_failure(self, adapter: Adapter, outcome: Outcome, attempt: int) -> None:
        blocked = outcome in (Outcome.BLOCKED, Outcome.RATE_LIMITED)
        if blocked and self.broker is not None and await self.broker.wait_for_human(adapter.site):
            self.scheduler.report(adapter.site, blocked=False)
            return  # user cleared the challenge in the visible window; retry at once
        self.scheduler.report(adapter.site, blocked=blocked)
        if self.broker is not None and (blocked or attempt >= 2):
            await self.broker.rotate(adapter.site)
        await asyncio.sleep(min(2**attempt * 5, 60) if not self.scheduler._fast else 0)

    def _interpret(self, adapter: Adapter, q: Query, raw: RawResponse, fid: str) -> QueryResult:
        prov = Provenance(
            adapter=adapter.id,
            tier=raw.tier,
            fetch_id=fid,
            observed_at=raw.started_at,
            request_pax=q.pax,
        )
        try:
            parsed = adapter.parse(raw, q, prov)
        except AdapterError as exc:
            return QueryResult(q, "failed", fetch_id=fid, reason=f"parse: {exc}")
        except Exception as exc:
            return QueryResult(q, "failed", fetch_id=fid, reason=f"parse crash: {exc!r}"[:500])
        if parsed.empty:
            if adapter.is_empty_answer(raw):
                return QueryResult(q, "empty", fetch_id=fid, parsed=parsed)
            return QueryResult(
                q,
                "failed",
                fetch_id=fid,
                parsed=parsed,
                reason="no offers parsed and response not recognized as a genuine empty result; "
                + "; ".join(parsed.warnings[:3]),
            )
        self._store(parsed, q)
        return QueryResult(q, "ok", fetch_id=fid, parsed=parsed)

    def _store(self, parsed: ParseResult, q: Query) -> None:
        rows = []
        for o in [*parsed.awards, *parsed.cash]:
            it = o.itinerary
            is_award = isinstance(o, AwardOffer)
            rows.append(
                ObservationRow(
                    observed_at=o.provenance.observed_at,
                    search_id=self.search_id,
                    fetch_id=o.provenance.fetch_id,
                    kind="award" if is_award else "cash",
                    source=o.program if is_award else o.source,  # type: ignore[union-attr]
                    origin=it.origin,
                    destination=it.destination,
                    depart_date=it.departs_at.date().isoformat(),
                    flight_key=it.flight_key,
                    cabin=it.main_cabin.value,
                    miles=o.miles if is_award else None,  # type: ignore[union-attr]
                    cash=float(o.taxes.amount if is_award else o.price.amount),  # type: ignore[union-attr]
                    currency=(o.taxes if is_award else o.price).currency,  # type: ignore[union-attr]
                    seats_kind=o.seats.kind if o.seats else None,
                    seats=o.seats.count if o.seats else None,
                    offer=o.model_dump(mode="json"),
                )
            )
        if rows:
            with session() as s:
                s.add_all(rows)


def offers_from_rows(rows: list[ObservationRow]) -> list[AwardOffer | CashOffer]:
    out: list[AwardOffer | CashOffer] = []
    for r in rows:
        out.append(
            AwardOffer.model_validate(r.offer)
            if r.kind == "award"
            else CashOffer.model_validate(r.offer)
        )
    return out


def now() -> datetime:
    return datetime.now(UTC)
