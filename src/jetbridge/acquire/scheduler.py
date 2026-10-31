"""Politeness scheduling: per-site pacing with jitter + circuit breaker.

Sites are independent (run in parallel); requests *within* a site are
serialized and spaced out. On repeated blocks the breaker opens and the site
cools down, after which the broker rotates the browser session.
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
    concurrency: int = 1
    daily_budget: int = 1500
    breaker_threshold: int = 3  # consecutive blocks before opening
    cooldown_s: float = 180.0


@dataclass
class _SiteState:
    policy: SitePolicy
    sem: asyncio.Semaphore
    last: float = 0.0
    used: int = 0
    consecutive_blocks: int = 0
    open_until: float = 0.0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


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
            self._sites[site] = _SiteState(policy=pol, sem=asyncio.Semaphore(pol.concurrency))
        return self._sites[site]

    def slot(self, site: str) -> _Slot:
        return _Slot(self, self._state(site), site)

    def report(self, site: str, *, blocked: bool) -> None:
        st = self._state(site)
        if blocked:
            st.consecutive_blocks += 1
            if st.consecutive_blocks >= st.policy.breaker_threshold:
                st.open_until = time.monotonic() + st.policy.cooldown_s
                st.consecutive_blocks = 0
        else:
            st.consecutive_blocks = 0

    def is_open(self, site: str) -> float:
        st = self._state(site)
        return max(0.0, st.open_until - time.monotonic())

    def usage(self) -> dict[str, int]:
        return {k: v.used for k, v in self._sites.items()}


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
        await st.sem.acquire()
        async with st.lock:
            if not self.sched._fast:
                gap = random.uniform(st.policy.min_interval_s, st.policy.max_interval_s)
                wait = st.last + gap - time.monotonic()
                if wait > 0:
                    await asyncio.sleep(wait)
            st.last = time.monotonic()
            st.used += 1

    async def __aexit__(self, *exc: object) -> None:
        self.st.last = time.monotonic()
        self.st.sem.release()
