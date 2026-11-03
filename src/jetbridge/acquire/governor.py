"""Rate governor: request budget and block cooldowns, persisted across processes.

From the project's own telemetry (2026-10-31), aa.com challenges after short bursts, Delta
blocked after about 85 requests in 2 minutes, and bot managers score both volume over a window
and persistence after a challenge. So:

* A sliding-window budget per site, counted in the database.
* No retry after a block: one block rests that site for 10 minutes, doubling per episode
  within 6 hours, up to 4 hours. Queries during a rest are deferred. Other sites carry on.
* The first request after a rest is the only one in flight (ticks are sequential and
  locked), and its verdict decides whether the rest ends or doubles.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass

from jetbridge.store import Store


@dataclass(frozen=True)
class Budget:
    window_s: int
    max_requests: int


# A watcher needs far less than these sites tolerated in testing, so these sit well below it:
# united went 669 requests unblocked at about 0.4 per second, and delta blocked at about 0.6.
BUDGETS = {
    "american": Budget(window_s=600, max_requests=10),
    "delta": Budget(window_s=300, max_requests=20),
    "united": Budget(window_s=300, max_requests=30),
}
# However a site's short window fills up, no site gets more than this in any 24 hours.
DAILY_LIMIT = 150
# Planned searching stays under that, leaving room for manual checks and re-checks of openings. A
# block halves it for a day, then it recovers in steps while there are no more.
PLANNED_DAILY = 120
RECOVERY_S = (86400.0, 2 * 86400.0)
DAY_S = 86400.0
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
    ):
        self.store, self.clock, self.sleep = store, clock, sleep

    def budget(self, site: str) -> Budget:
        return BUDGETS[site]

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

    def used_today(self, site: str) -> int:
        count, _ = self.store.window_starts(site, self.clock() - DAY_S)
        return count

    def planned_today(self, site: str) -> int:
        """How many requests planned searching may use in 24 hours, given recent blocks."""
        last = self.health(site).last_block
        since = self.clock() - last
        if last and since < RECOVERY_S[0]:
            return PLANNED_DAILY // 2
        if last and since < RECOVERY_S[1]:
            return PLANNED_DAILY * 3 // 4
        return PLANNED_DAILY

    def room_today(self, site: str) -> int:
        return max(0, self.planned_today(site) - self.used_today(site))

    def used(self, site: str) -> int:
        count, _ = self.store.window_starts(site, self.clock() - self.budget(site).window_s)
        return count

    async def acquire(self, site: str, deadline: float) -> None:
        """Wait for permission to start one request, or raise ``GovernorDeferred``.

        A rest period is never waited out inside a tick; a full budget is waited out only
        if it frees up before ``deadline``.
        """
        budget = self.budget(site)
        while True:
            now = self.clock()
            until = self.cooldown_until(site)
            if until > now:
                raise GovernorDeferred(site, "resting after a block", until)
            today_count, today_oldest = self.store.window_starts(site, now - DAY_S)
            if today_count >= DAILY_LIMIT:
                # A day is too long to wait inside a tick, so this always hands back to the caller.
                free_at = (today_oldest if today_oldest is not None else now) + DAY_S + 0.05
                raise GovernorDeferred(site, "daily search limit reached", free_at)
            count, oldest = self.store.window_starts(site, now - budget.window_s)
            if count < budget.max_requests:
                self.store.record_start(site, now)
                return
            free_at = (oldest if oldest is not None else now) + budget.window_s + 0.05
            if free_at > deadline:
                raise GovernorDeferred(site, "request budget used", free_at)
            await self.sleep(max(0.05, min(free_at - now, 30)))
