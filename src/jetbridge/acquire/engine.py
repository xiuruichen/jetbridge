"""Acquisition engine: executes Queries with pacing, retries, block handling,
provenance and persistence.

Outcome semantics (what the coverage ledger records):
  ok       genuine response with >=1 offer
  empty    genuine response, positively recognized as "no flights"
  blocked  bot-manager challenge / 403 (never retried: the whole site cools down)
  deferred could not start inside the time budget (site cooling / request budget)
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
from jetbridge.acquire.cache import cached_any, record_fetch
from jetbridge.acquire.detect import classify
from jetbridge.acquire.freshness import serving_queries, ttl_for
from jetbridge.acquire.governor import Governor, GovernorDeferred
from jetbridge.acquire.scheduler import BreakerOpen, BudgetExhausted, Scheduler, SitePolicy
from jetbridge.acquire.types import AdapterError, Outcome, ParseResult, Query, RawResponse
from jetbridge.models.core import AwardOffer, CashOffer, Provenance
from jetbridge.store import availability
from jetbridge.store.db import ObservationRow, session

log = logging.getLogger("jetbridge.engine")


@dataclass
class QueryResult:
    query: Query
    status: str  # ok|empty|blocked|failed|skipped|deferred
    fetch_id: str | None = None
    parsed: ParseResult = field(default_factory=ParseResult)
    reason: str | None = None
    attempts: int = 0
    from_cache: bool = False


# In-process concurrency + spacing between request starts, per site (AIMD).
# The cross-process *volume* limit lives in governor.py; this only shapes bursts.
DEFAULT_POLICIES = {
    "united": SitePolicy(
        min_interval_s=0.8, max_interval_s=1.6, concurrency=4, start_concurrency=2
    ),
    "american": SitePolicy(
        min_interval_s=4.0, max_interval_s=9.0, concurrency=1, start_concurrency=1, cooldown_s=600
    ),
    "delta": SitePolicy(min_interval_s=3.0, max_interval_s=7.0, concurrency=2, start_concurrency=1),
    "alaska": SitePolicy(
        min_interval_s=0.6, max_interval_s=1.2, concurrency=4, start_concurrency=2
    ),
    "google": SitePolicy(
        min_interval_s=1.0, max_interval_s=2.0, concurrency=3, start_concurrency=2
    ),
}


def site_adapters(adapters: dict[str, Adapter]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for a in adapters.values():
        out.setdefault(a.site, []).append(a.id)
    return out


class Engine:
    def __init__(
        self,
        adapters: dict[str, Adapter],
        broker: SessionBroker | None,
        scheduler: Scheduler | None = None,
        *,
        cache_ttl: timedelta | None = None,
        max_attempts: int = 3,
        search_id: str | None = None,
        logins_enabled: frozenset[str] = frozenset(),
        deadline: float | None = None,
        governor: Governor | None = None,
        role: str = "search",
    ):
        self.adapters = adapters
        self.broker = broker
        self.scheduler = scheduler or Scheduler(DEFAULT_POLICIES)
        # None = per-query freshness (freshness.ttl_for); timedelta(0) = never cache
        self.cache_ttl = cache_ttl
        self.max_attempts = max_attempts
        self.search_id = search_id
        self.logins_enabled = logins_enabled
        self.deadline = deadline  # time.monotonic() wall-clock limit for waits
        self.governor = governor or Governor(site_adapters=site_adapters(adapters))
        self.role = role
        self.stats: dict[str, int] = {"live": 0, "cached": 0, "deferred": 0, "blocked": 0}

    @property
    def human_present(self) -> bool:
        return self.broker is not None and not getattr(self.broker, "offscreen", True)

    def _epoch_deadline(self) -> float | None:
        if self.deadline is None:
            return None
        return time.time() + (self.deadline - time.monotonic())

    def lookup_cache(self, q: Query) -> QueryResult | None:
        """A fresh stored answer for ``q`` (party-size dominance aware), or None."""
        adapter = self.adapters[q.adapter]
        ttl = self.cache_ttl if self.cache_ttl is not None else ttl_for(q, adapter)
        if ttl <= timedelta(0):
            return None
        hit = cached_any(serving_queries(q, adapter), ttl)
        if hit is None:
            return None
        fid, raw, answered = hit
        res = self._interpret(adapter, answered, raw, fid, ingest=False)
        if res.status not in ("ok", "empty"):
            return None  # a stored answer that no longer parses is not an answer
        res.from_cache = True
        if answered.pax != q.pax:
            res.reason = (
                f"served from a {answered.pax}-pax answer (price independent of party size, "
                "exact seat counts)"
            )
        return res

    async def run(self, q: Query, *, use_cache: bool = True) -> QueryResult:
        adapter = self.adapters[q.adapter]
        if adapter.capabilities.needs_login and adapter.id not in self.logins_enabled:
            return QueryResult(
                q,
                "skipped",
                reason=f"{adapter.id} requires sign-in; not enabled in profile (logins_enabled)",
            )
        if use_cache:
            hit = self.lookup_cache(q)
            if hit is not None:
                self.stats["cached"] += 1
                return hit
        if self.broker is None:
            return QueryResult(q, "failed", reason="no browser broker (offline mode)")

        last_reason = "not attempted"
        last_outcome = Outcome.ERROR
        attempt = 0
        for attempt in range(1, self.max_attempts + 1):
            try:
                async with self.scheduler.slot(adapter.site):
                    await self.governor.acquire(
                        adapter.site,
                        role=self.role,
                        deadline=self._epoch_deadline(),
                        honor_cooldown=not self.human_present,
                        wait_cooldown=self.role == "crawl",
                    )
                    self.stats["live"] += 1
                    try:
                        if adapter.exclusive_tab:
                            conc = self.scheduler.policy(adapter.site).concurrency
                            async with self.broker.tab(
                                adapter.site, adapter.warmup_url, conc
                            ) as page:
                                raw = await self._bounded(adapter.fetch(page, q), adapter)
                        else:
                            page = await self.broker.page(adapter.site, adapter.warmup_url)
                            raw = await self._bounded(adapter.fetch(page, q), adapter)
                    except BaseException:
                        self.governor.release_probe(adapter.site)
                        raise
            except GovernorDeferred as exc:
                self.stats["deferred"] += 1
                return QueryResult(
                    q,
                    "deferred",
                    reason=f"deferred: {exc}; the background crawler or "
                    "`jetbridge resume <search_id>` will fill it",
                    attempts=attempt - 1,
                )
            except BreakerOpen as exc:
                if self._past(exc.retry_in):
                    return QueryResult(
                        q,
                        "deferred",
                        reason=f"deferred: {exc} (cooldown would exceed the time budget)",
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
                if await self._after_failure(adapter, exc.outcome, attempt):
                    continue
                break
            except Exception as exc:  # browser crash, navigation timeout, ...
                last_reason, last_outcome = f"{type(exc).__name__}: {exc}"[:500], Outcome.ERROR
                record_fetch(q, None, Outcome.ERROR, last_reason)
                await self._after_failure(adapter, Outcome.ERROR, attempt)
                continue

            outcome, detail = classify(raw, require_marker=adapter.marker)
            if outcome is Outcome.OK:
                fid = record_fetch(q, raw, Outcome.OK)
                self.scheduler.report(adapter.site, blocked=False)
                self.governor.record_ok(adapter.site)
                res = self._interpret(adapter, q, raw, fid)
                res.attempts = attempt
                return res
            last_reason, last_outcome = detail or outcome.value, outcome
            record_fetch(q, raw, outcome, detail)
            if not await self._after_failure(adapter, outcome, attempt):
                break

        status = "blocked" if last_outcome in (Outcome.BLOCKED, Outcome.RATE_LIMITED) else "failed"
        if status == "blocked":
            self.stats["blocked"] += 1
            cool = self.governor.cooldown_remaining(adapter.site)
            if cool > 0:
                last_reason += (
                    f"; {adapter.site} is cooling down for {cool / 60:.0f} min (no retries into a "
                    "challenge). `jetbridge resume <search_id> --show-browser` lets you clear it by hand"
                )
        return QueryResult(q, status, reason=last_reason, attempts=attempt)

    async def _after_failure(self, adapter: Adapter, outcome: Outcome, attempt: int) -> bool:
        """Handle a failed attempt; returns whether another attempt makes sense."""
        blocked = outcome in (Outcome.BLOCKED, Outcome.RATE_LIMITED)
        if blocked:
            if self.broker is not None and await self.broker.wait_for_human(adapter.site):
                self.scheduler.report(adapter.site, blocked=False)
                return True  # the user cleared the challenge in the visible window
            # Retrying into a challenge deepens the flag. Cool the whole site down
            # (persisted for every process) and start fresh after it.
            self.scheduler.report(adapter.site, blocked=True)
            length = self.governor.record_block(adapter.site)
            log.warning("%s blocked: site cooldown %.0f min", adapter.site, length / 60)
            if self.broker is not None:
                await self.broker.rotate(adapter.site)
            return False
        self.scheduler.report(adapter.site, blocked=False)
        if self.broker is not None and attempt >= 2:
            await self.broker.rotate(adapter.site)
        backoff = min(2**attempt * 5, 60) if not self.scheduler._fast else 0
        if self.deadline is not None:
            backoff = max(0.0, min(backoff, self.deadline - time.monotonic()))
        await asyncio.sleep(backoff)
        return True

    async def _bounded(self, coro, adapter: Adapter):  # type: ignore[no-untyped-def]
        """Hard wall-clock bound on one fetch: a hung browser call becomes an error
        (retried within the deadline) instead of stalling the whole run."""
        limit = adapter.request_timeout_ms / 1000 + 30
        if self.deadline is not None:
            limit = max(5.0, min(limit, self.deadline - time.monotonic() + 15))
        return await asyncio.wait_for(coro, timeout=limit)

    def _past(self, extra_s: float = 0.0) -> bool:
        return self.deadline is not None and time.monotonic() + extra_s > self.deadline

    def _interpret(
        self, adapter: Adapter, q: Query, raw: RawResponse, fid: str, *, ingest: bool = True
    ) -> QueryResult:
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
                if ingest:
                    self._ingest(adapter, q, parsed, fid, raw, empty=True)
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
        if ingest:
            self._ingest(adapter, q, parsed, fid, raw, empty=False)
        return QueryResult(q, "ok", fetch_id=fid, parsed=parsed)

    def _ingest(
        self,
        adapter: Adapter,
        q: Query,
        parsed: ParseResult,
        fid: str,
        raw: RawResponse,
        *,
        empty: bool,
    ) -> None:
        if adapter.program is None:
            return  # cash sources are not availability cells
        try:
            alerts = availability.ingest(
                q, adapter.program, parsed, empty=empty, fetch_id=fid, observed_at=raw.started_at
            )
            for a in alerts:
                log.info(
                    "ALERT %s %s %s-%s %s %s",
                    a.kind,
                    a.program,
                    a.origin,
                    a.destination,
                    a.depart_date,
                    a.cabin,
                )
        except Exception as exc:  # the store is derived data: never fail a query on it
            log.warning("availability ingest failed for %s: %r", q.key, exc)

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
