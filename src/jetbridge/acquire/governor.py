"""Rate governor: request budget and block cooldowns, persisted across processes.

From the project's own telemetry (2026-10-31), aa.com challenges after short bursts, and
bot managers score both volume over a window and persistence after a challenge. So:

* A sliding-window budget counted in the database.
* No retry after a block: one block rests the site for 10 minutes, doubling per episode
  within 6 hours, up to 4 hours. Queries during a rest are deferred.
* The first request after a rest is the only one in flight (ticks are sequential and
  locked), and its verdict decides whether the rest ends or doubles.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from jetbridge.store import Store

WINDOW_S = 600
MAX_REQUESTS = 10
COOLDOWN_BASE_S = 600.0
COOLDOWN_MAX_S = 4 * 3600.0
EPISODE_MEMORY_S = 6 * 3600.0


class GovernorDeferred(Exception):
    """The request may not start before the caller's deadline."""

    def __init__(self, site: str, why: str, retry_at: float):
        super().__init__(f"{site}: {why}")
        self.site, self.why, self.retry_at = site, why, retry_at


@dataclass
class SiteHealth:
    episodes: int = 0
    last_block: float = 0.0
    cooldown_until: float = 0.0
    needs_probe: bool = False


class Governor:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        *,
        window_s: int = WINDOW_S,
        max_requests: int = MAX_REQUESTS,
    ):
        self.store, self.clock, self.sleep = store, clock, sleep
        self.window_s, self.max_requests = window_s, max_requests

    def health(self, site: str) -> SiteHealth:
        raw = self.store.get(f"governor:{site}")
        return SiteHealth(**json.loads(raw)) if raw else SiteHealth()

    def _save(self, site: str, health: SiteHealth) -> None:
        self.store.set(f"governor:{site}", json.dumps(asdict(health)))

    def cooldown_until(self, site: str) -> float:
        return self.health(site).cooldown_until

    def record_block(self, site: str) -> float:
        """Open or extend the rest period. Returns when it ends."""
        now = self.clock()
        health = self.health(site)
        recent = now - health.last_block < EPISODE_MEMORY_S
        health.episodes = health.episodes + 1 if recent else 1
        length = min(COOLDOWN_BASE_S * 2 ** (health.episodes - 1), COOLDOWN_MAX_S)
        health.last_block = now
        health.cooldown_until = max(health.cooldown_until, now + length)
        health.needs_probe = True
        self._save(site, health)
        return health.cooldown_until

    def record_ok(self, site: str) -> None:
        health = self.health(site)
        stale = health.episodes and self.clock() - health.last_block > EPISODE_MEMORY_S
        if health.needs_probe or stale:
            health.needs_probe = False
            if stale:
                health.episodes = 0
            self._save(site, health)

    def used(self, site: str) -> int:
        count, _ = self.store.window_starts(site, self.clock() - self.window_s)
        return count

    async def acquire(self, site: str, deadline: float) -> None:
        """Wait for permission to start one request, or raise ``GovernorDeferred``.

        A rest period is never waited out inside a tick; a full budget is waited out only
        if it frees up before ``deadline``.
        """
        while True:
            now = self.clock()
            until = self.cooldown_until(site)
            if until > now:
                raise GovernorDeferred(site, "resting after a block", until)
            count, oldest = self.store.window_starts(site, now - self.window_s)
            if count < self.max_requests:
                self.store.record_start(site, now)
                return
            free_at = (oldest if oldest is not None else now) + self.window_s + 0.05
            if free_at > deadline:
                raise GovernorDeferred(site, "request budget used", free_at)
            await self.sleep(max(0.05, min(free_at - now, 30)))
