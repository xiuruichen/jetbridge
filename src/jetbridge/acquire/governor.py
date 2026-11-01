"""Rate governor: per-site request budgets and block cooldowns shared by every process.

Why this exists (from our own telemetry, 2026-10-31)
--------------------------------------------------
Delta answered ~85 GraphQL requests in ~2 minutes and then Akamai returned 444 for
the next 50 minutes. Every blocked query had been retried up to 3x with session
rotation, and each new run started with a fresh in-memory scheduler and
immediately probed again, so the site never cooled down. American behaved the
same. Bot managers score *volume over a window* and *persistence after a
challenge*, so the fix is not a better browser but a different traffic shape:

* **Sliding-window budget** per site (``max_requests`` per ``window_s``),
  counted from the ``site_starts`` table, so it holds across concurrent
  processes (a search and the background crawler) and across runs.
* **Learned caps.** The block history in ``fetches`` is mined: for every
  block that followed a run of good responses, the number of requests in the
  preceding window is an observed "too many". The cap becomes at most 60% of
  the smallest such count (never below a floor).
* **No retry-after-block.** One block puts the whole site into an escalating,
  persisted cooldown (10 min, doubling per episode within 6 h, up to 4 h).
  Queries that would run during it are *deferred* (the crawler or a resume
  picks them up), never fired into the challenge.
* **Probation.** After a cooldown the first request is a lone probe; the rest
  of the site's traffic waits for its verdict.
* **Shares.** The crawler uses only part of each window so an interactive
  search always has headroom.
"""

from __future__ import annotations

import asyncio
import json
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select

from jetbridge.store.db import FetchRow, KVRow, SiteStartRow, session


@dataclass(frozen=True)
class Budget:
    window_s: int
    max_requests: int
    floor: int = 3  # a learned cap never goes below this


# Defaults from observed behaviour (docs/sources/README.md and fetch telemetry):
# united and alaska never blocked at ~0.4-0.6 q/s, delta blocked at ~0.6 q/s,
# american challenges after short bursts.
DEFAULT_BUDGETS: dict[str, Budget] = {
    "united": Budget(window_s=300, max_requests=110, floor=20),
    "alaska": Budget(window_s=300, max_requests=140, floor=20),
    "delta": Budget(window_s=300, max_requests=30, floor=6),
    "american": Budget(window_s=600, max_requests=10, floor=3),
    "google": Budget(window_s=300, max_requests=60, floor=10),
}

ROLE_SHARE = {"search": 1.0, "verify": 1.0, "crawl": 0.5}
COOLDOWN_BASE_S = 600.0
COOLDOWN_MAX_S = 4 * 3600.0
EPISODE_MEMORY_S = 6 * 3600.0
LEARN_LOOKBACK = timedelta(days=14)
LEARN_SAFETY = 0.6


class GovernorDeferred(Exception):
    """The request cannot start within the caller's deadline (cooldown/window/probe)."""

    def __init__(self, site: str, why: str, retry_at: float | None = None):
        self.site, self.why, self.retry_at = site, why, retry_at
        when = ""
        if retry_at:
            when = " until ~" + datetime.fromtimestamp(retry_at).strftime("%H:%M")
        super().__init__(f"{site}: {why}{when}")


@dataclass
class SiteHealth:
    episodes: int = 0
    last_block: float = 0.0
    cooldown_until: float = 0.0
    needs_probe: bool = False
    last_ok: float = 0.0

    def to_json(self) -> str:
        return json.dumps(self.__dict__)

    @classmethod
    def from_json(cls, s: str | None) -> SiteHealth:
        return cls(**json.loads(s)) if s else cls()


class Governor:
    def __init__(
        self,
        budgets: dict[str, Budget] | None = None,
        *,
        site_adapters: dict[str, list[str]] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        learn: bool = True,
    ):
        self.budgets = dict(DEFAULT_BUDGETS if budgets is None else budgets)
        self.site_adapters = site_adapters or {}
        self.clock = clock
        self.sleep = sleep
        self._learned: dict[str, int] = {}
        self._learn = learn
        self._probe_inflight: set[str] = set()
        self._last_prune = 0.0

    # ------------------------------------------------------------------ health
    def health(self, site: str) -> SiteHealth:
        with session() as s:
            row = s.get(KVRow, f"governor:{site}")
            return SiteHealth.from_json(row.value if row else None)

    def _save(self, site: str, h: SiteHealth) -> None:
        with session() as s:
            s.merge(KVRow(key=f"governor:{site}", value=h.to_json()))

    def cooldown_remaining(self, site: str) -> float:
        return max(0.0, self.health(site).cooldown_until - self.clock())

    def record_block(self, site: str) -> float:
        """Open (or extend) the site's cooldown; returns its length in seconds."""
        now = self.clock()
        h = self.health(site)
        h.episodes = h.episodes + 1 if now - h.last_block < EPISODE_MEMORY_S else 1
        length = min(COOLDOWN_BASE_S * 2 ** (h.episodes - 1), COOLDOWN_MAX_S)
        h.last_block = now
        h.cooldown_until = max(h.cooldown_until, now + length)
        h.needs_probe = True
        self._save(site, h)
        self._probe_inflight.discard(site)
        return length

    def record_ok(self, site: str) -> None:
        self._probe_inflight.discard(site)
        h = self.health(site)
        stale_episode = h.episodes and self.clock() - h.last_block > EPISODE_MEMORY_S
        if h.needs_probe or stale_episode:
            h.needs_probe = False
            h.episodes = 0 if stale_episode else h.episodes
            h.last_ok = self.clock()
            self._save(site, h)

    def release_probe(self, site: str) -> None:
        """A probe ended without a verdict (error/timeout): let another one try."""
        self._probe_inflight.discard(site)

    # ------------------------------------------------------------------ budget
    def cap(self, site: str, role: str = "search") -> int | None:
        b = self.budgets.get(site)
        if b is None:
            return None
        cap = b.max_requests
        learned = self.learned_cap(site)
        if learned is not None:
            cap = min(cap, max(learned, b.floor))
        return max(1, math.floor(cap * ROLE_SHARE.get(role, 1.0)))

    def learned_cap(self, site: str) -> int | None:
        if not self._learn or site not in self.budgets:
            return None
        if site in self._learned:
            return self._learned[site] or None
        b = self.budgets[site]
        adapters = self.site_adapters.get(site, [site])
        since = datetime.now(UTC) - LEARN_LOOKBACK
        try:
            with session() as s:
                rows = list(
                    s.execute(
                        select(FetchRow.started_at, FetchRow.outcome)
                        .where(
                            FetchRow.adapter.in_(adapters),
                            FetchRow.tier != "replay",
                            FetchRow.started_at >= since,
                        )
                        .order_by(FetchRow.started_at)
                    )
                )
        except Exception:
            rows = []
        learned = learn_cap_from_history(
            [(_epoch(t), o) for t, o in rows], window_s=b.window_s, floor=b.floor
        )
        self._learned[site] = learned or 0
        return learned

    def window_count(self, site: str, window_s: int) -> tuple[int, float | None]:
        """(# request starts in the window, epoch of the oldest one in it)."""
        cutoff = self.clock() - window_s
        with session() as s:
            n, oldest = s.execute(
                select(func.count(SiteStartRow.id), func.min(SiteStartRow.started_at)).where(
                    SiteStartRow.site == site, SiteStartRow.started_at >= cutoff
                )
            ).one()
        return int(n), oldest

    def usage(self) -> dict[str, dict]:
        out = {}
        for site, b in self.budgets.items():
            n, _ = self.window_count(site, b.window_s)
            h = self.health(site)
            out[site] = {
                "window_s": b.window_s,
                "used": n,
                "cap": self.cap(site),
                "learned_cap": self.learned_cap(site),
                "cooldown_s": round(max(0.0, h.cooldown_until - self.clock())),
                "block_episodes": h.episodes,
                "needs_probe": h.needs_probe,
            }
        return out

    # ------------------------------------------------------------------ acquire
    async def acquire(
        self,
        site: str,
        *,
        role: str = "search",
        deadline: float | None = None,
        honor_cooldown: bool = True,
        wait_cooldown: bool = False,
    ) -> None:
        """Wait until a request to ``site`` may start, then record the start.

        ``deadline`` is an epoch time; if the request could not start before it,
        :class:`GovernorDeferred` is raised instead of waiting. A cooldown is only
        waited out when ``wait_cooldown`` (the crawler); a search defers at once."""
        b = self.budgets.get(site)
        while True:
            now = self.clock()
            h = self.health(site)
            if honor_cooldown and h.cooldown_until > now:
                self._wait_or_defer(
                    site,
                    "cooling down after a block",
                    h.cooldown_until,
                    deadline if wait_cooldown else now,
                )
                await self.sleep(min(h.cooldown_until - now, 30))
                continue
            if honor_cooldown and h.needs_probe and site in self._probe_inflight:
                self._wait_or_defer(site, "waiting for the post-cooldown probe", now + 5, deadline)
                await self.sleep(1.0)
                continue
            if b is not None:
                cap = self.cap(site, role) or b.max_requests
                n, oldest = self.window_count(site, b.window_s)
                if n >= cap:
                    free_at = (oldest or now) + b.window_s + 0.05
                    self._wait_or_defer(
                        site,
                        f"request budget ({cap}/{b.window_s // 60} min) used",
                        free_at,
                        deadline,
                    )
                    await self.sleep(max(0.05, min(free_at - now, 30)))
                    continue
            if h.needs_probe:
                self._probe_inflight.add(site)
            with session() as s:
                s.add(SiteStartRow(site=site, started_at=now, role=role))
            self._maybe_prune()
            return

    def _wait_or_defer(self, site: str, why: str, until: float, deadline: float | None) -> None:
        if deadline is not None and until > deadline:
            raise GovernorDeferred(site, why, until)

    def _maybe_prune(self) -> None:
        now = self.clock()
        if now - self._last_prune < 600:
            return
        self._last_prune = now
        with session() as s:
            s.execute(delete(SiteStartRow).where(SiteStartRow.started_at < now - 86400))


def learn_cap_from_history(
    events: list[tuple[float, str]], *, window_s: int, floor: int
) -> int | None:
    """Cap implied by past burst-induced blocks.

    ``events`` are (epoch, outcome) in time order. A *block onset* is a blocked
    or rate-limited fetch whose previous fetch succeeded. If the window before an
    onset held more than ``floor`` requests, that volume tripped the bot manager;
    we keep ``LEARN_SAFETY`` of the smallest such volume. Onsets with little
    traffic before them were not caused by rate (a flagged profile) and teach
    nothing about the cap."""
    blocked = {"blocked", "rate_limited"}
    good = {"ok", "empty"}
    caps: list[int] = []
    times = [t for t, _ in events]
    prev = None
    lo = 0
    for i, (t, outcome) in enumerate(events):
        while times[lo] < t - window_s:
            lo += 1
        if outcome in blocked and prev in good:
            n = i - lo  # requests strictly before the onset within the window
            if n > floor:
                caps.append(max(floor, math.floor(n * LEARN_SAFETY)))
        if outcome in good or outcome in blocked:
            prev = outcome
    return min(caps) if caps else None


def _epoch(t: datetime) -> float:
    return (t if t.tzinfo else t.replace(tzinfo=UTC)).timestamp()
