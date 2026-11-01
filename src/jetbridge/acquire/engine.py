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
import time
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

# Concurrency + spacing between request starts, per site. T1 sites (in-page
# fetch) overlap requests inside one warmed tab; T3 sites get a tab per slot.
# American is the most bot-sensitive (interactive challenges observed), so it
# gets the gentlest policy.
DEFAULT_POLICIES = {
    # concurrency = MAX in flight; AIMD starts at start_concurrency and adapts.
    # Delta blocked at 4-way / ~0.5 q/s in a live run (2026-10-31): cap 3, start 2.
    "united": SitePolicy(
        min_interval_s=0.8, max_interval_s=1.6, concurrency=4, start_concurrency=2
    ),
    "american": SitePolicy(
        min_interval_s=2.0, max_interval_s=4.0, concurrency=2, start_concurrency=1, cooldown_s=600
    ),
    "delta": SitePolicy(min_interval_s=1.0, max_interval_s=2.0, concurrency=3, start_concurrency=2),
    "alaska": SitePolicy(
        min_interval_s=0.6, max_interval_s=1.2, concurrency=4, start_concurrency=2
    ),
    "google": SitePolicy(
        min_interval_s=1.0, max_interval_s=2.0, concurrency=3, start_concurrency=2
    ),
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
        deadline: float | None = None,
    ):
        self.adapters = adapters
        self.broker = broker
        self.scheduler = scheduler or Scheduler(DEFAULT_POLICIES)
        self.cache_ttl = cache_ttl
        self.max_attempts = max_attempts
        self.search_id = search_id
        self.logins_enabled = logins_enabled
        self.deadline = deadline  # time.monotonic() wall-clock limit for waits
        # Site quarantine: after QUARANTINE_AFTER consecutive *queries* end blocked
        # (each already retried), stop spending time on that site for this run when
        # no human can clear the challenge. Remaining queries are recorded as
        # blocked with a remedy, and `resume --show-browser` retries them.
        self._block_streak: dict[str, int] = {}
        self.quarantined: dict[str, str] = {}
        if broker is not None and getattr(broker, "offscreen", False):
            self._prequarantine()

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
                    if adapter.exclusive_tab:
                        conc = self.scheduler.policy(adapter.site).concurrency
                        async with self.broker.tab(adapter.site, adapter.warmup_url, conc) as page:
                            raw = await self._bounded(adapter.fetch(page, q), adapter)
                    else:
                        page = await self.broker.page(adapter.site, adapter.warmup_url)
                        raw = await self._bounded(adapter.fetch(page, q), adapter)
            except BreakerOpen as exc:
                if self._past(exc.retry_in):
                    return QueryResult(
                        q,
                        "blocked",
                        reason=f"{exc} (cooldown would exceed the time budget)",
                        attempts=attempt,
                    )
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
        backoff = min(2**attempt * 5, 60) if not self.scheduler._fast else 0
        if self.deadline is not None:
            backoff = max(0.0, min(backoff, self.deadline - time.monotonic()))
        await asyncio.sleep(backoff)

    def _prequarantine(self, window_min: float = 15, last_n: int = 5) -> None:
        """Carry block state across runs: a site whose last ``last_n`` live fetches in
        the past ``window_min`` minutes were all blocks starts this run quarantined."""
        from datetime import timedelta as _td

        from sqlalchemy import select as _select

        from jetbridge.store.db import FetchRow, session

        since = datetime.now(UTC) - _td(minutes=window_min)
        try:
            with session() as s:
                for a in self.adapters.values():
                    outs = list(
                        s.scalars(
                            _select(FetchRow.outcome)
                            .where(FetchRow.adapter == a.id, FetchRow.started_at >= since)
                            .order_by(FetchRow.started_at.desc())
                            .limit(last_n)
                        )
                    )
                    if len(outs) == last_n and all(o in ("blocked", "rate_limited") for o in outs):
                        self.quarantined[a.site] = (
                            f"{a.site} was blocking in the last {window_min:g} min; skipped. Run "
                            "`jetbridge resume <search_id> --show-browser` (complete the check) or retry later."
                        )
                        log.warning("pre-quarantined %s from recent telemetry", a.site)
        except Exception:
            pass

    async def _bounded(self, coro, adapter: Adapter):  # type: ignore[no-untyped-def]
        """Hard wall-clock bound on one fetch: a hung browser call becomes an error
        (retried within the deadline) instead of stalling the whole run."""
        limit = adapter.request_timeout_ms / 1000 + 30
        if self.deadline is not None:
            limit = max(5.0, min(limit, self.deadline - time.monotonic() + 15))
        return await asyncio.wait_for(coro, timeout=limit)

    def _past(self, extra_s: float = 0.0) -> bool:
        return self.deadline is not None and time.monotonic() + extra_s > self.deadline

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
