"""Politeness scheduling: adaptive per-site rate control + circuit breaker.

Sites are independent (run in parallel). Within a site, request STARTS are
spaced by a jittered gap and at most ``limit`` requests are in flight.

Adaptive control (AIMD, as in TCP congestion control):
  * additive increase — after ``increase_after`` consecutive clean responses,
    allow one more concurrent request (up to ``policy.concurrency``) and shrink
    the spacing multiplier back toward 1;
  * multiplicative decrease — on any block / rate-limit, halve the in-flight
    limit (min 1) and double the spacing multiplier (max 8x).
Each site therefore converges to the fastest rate it currently tolerates,
instead of a hand-tuned constant that is either too slow or gets us blocked.
Repeated blocks still open the circuit breaker (cooldown).
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field


@dataclass
class SitePolicy:
    min_interval_s: float = 3.0
    max_interval_s: float = 8.0
    concurrency: int = 1  # MAX in-flight; adaptive control starts at start_concurrency
    start_concurrency: int | None = None
    daily_budget: int = 3000
    breaker_threshold: int = 3  # consecutive blocks before opening
    cooldown_s: float = 180.0
    increase_after: int = 8  # clean responses before +1 concurrency


@dataclass
class _SiteState:
    policy: SitePolicy
    limit: int
    in_flight: int = 0
    spacing_mult: float = 1.0
    clean_streak: int = 0
    last: float = 0.0
    used: int = 0
    consecutive_blocks: int = 0
    open_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cond: asyncio.Condition = field(default_factory=asyncio.Condition)
    history: list[tuple[float, int, float]] = field(default_factory=list)  # (t, limit, mult)


class BreakerOpen(Exception):
    def __init__(self, site: str, retry_in: float):
        super().__init__(f"circuit open for {site}; retry in {retry_in:.0f}s")
        self.site = site
        self.retry_in = retry_in


class BudgetExhausted(Exception):
    pass


class Scheduler:
    def __init__(self, policies: dict[str, SitePolicy] | None = None, *, fast: bool = False):
        self._policies = policies or {}
        self._sites: dict[str, _SiteState] = {}
        self._fast = fast  # tests / replay: no sleeping

    def _state(self, site: str) -> _SiteState:
        if site not in self._sites:
            pol = self._policies.get(site, SitePolicy())
            start = pol.start_concurrency or max(1, (pol.concurrency + 1) // 2)
            self._sites[site] = _SiteState(policy=pol, limit=min(start, pol.concurrency))
        return self._sites[site]

    def policy(self, site: str) -> SitePolicy:
        return self._state(site).policy

    def limit(self, site: str) -> int:
        return self._state(site).limit

    def slot(self, site: str) -> _Slot:
        return _Slot(self, self._state(site), site)

    def report(self, site: str, *, blocked: bool) -> None:
        st = self._state(site)
        if blocked:
            st.consecutive_blocks += 1
            st.clean_streak = 0
            st.limit = max(1, st.limit // 2)  # multiplicative decrease
            st.spacing_mult = min(8.0, st.spacing_mult * 2)
            if st.consecutive_blocks >= st.policy.breaker_threshold:
                st.open_until = time.monotonic() + st.policy.cooldown_s
                st.consecutive_blocks = 0
        else:
            st.consecutive_blocks = 0
            st.clean_streak += 1
            if st.clean_streak >= st.policy.increase_after:
                st.clean_streak = 0
                st.limit = min(st.policy.concurrency, st.limit + 1)  # additive increase
                st.spacing_mult = max(1.0, st.spacing_mult * 0.75)
        st.history.append((time.monotonic(), st.limit, st.spacing_mult))

    def is_open(self, site: str) -> float:
        st = self._state(site)
        return max(0.0, st.open_until - time.monotonic())

    def usage(self) -> dict[str, int]:
        return {k: v.used for k, v in self._sites.items()}

    def snapshot(self) -> dict[str, dict]:
        return {
            k: {"limit": v.limit, "spacing_mult": round(v.spacing_mult, 2), "used": v.used}
            for k, v in self._sites.items()
        }


class _Slot:
    def __init__(self, sched: Scheduler, st: _SiteState, site: str):
        self.sched, self.st, self.site = sched, st, site

    async def __aenter__(self) -> None:
        st = self.st
        retry = st.open_until - time.monotonic()
        if retry > 0:
            raise BreakerOpen(self.site, retry)
        if st.used >= st.policy.daily_budget:
            raise BudgetExhausted(f"{self.site} daily budget {st.policy.daily_budget} used")
        async with st.cond:  # wait for an in-flight slot under the *current* adaptive limit
            await st.cond.wait_for(lambda: st.in_flight < st.limit)
            st.in_flight += 1
        async with st.lock:
            if not self.sched._fast:
                gap = (
                    random.uniform(st.policy.min_interval_s, st.policy.max_interval_s)
                    * st.spacing_mult
                )
                wait = st.last + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            st.last = time.monotonic()
            st.used += 1

    async def __aexit__(self, *exc: object) -> None:
        # Spacing applies between request STARTS so concurrent requests overlap;
        # resetting `last` here would silently serialize the site.
        async with self.st.cond:
            self.st.in_flight -= 1
            self.st.cond.notify_all()
