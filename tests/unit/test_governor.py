"""Rate governor: cross-process window budgets, learned caps, cooldown, probation."""

from __future__ import annotations

import pytest

from jetbridge.acquire.governor import (
    COOLDOWN_BASE_S,
    Budget,
    Governor,
    GovernorDeferred,
    learn_cap_from_history,
)


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_VAR", str(tmp_path))
    from jetbridge.store import db

    db._engines.clear()


class Clock:
    def __init__(self, t: float = 1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    async def sleep(self, s: float) -> None:
        self.t += s


def gov(clock: Clock, **budgets: Budget) -> Governor:
    return Governor(budgets, clock=clock, sleep=clock.sleep, learn=False)


async def test_window_budget_waits_then_defers_past_deadline():
    c = Clock()
    g = gov(c, s=Budget(window_s=60, max_requests=3, floor=1))
    for _ in range(3):
        await g.acquire("s")
    with pytest.raises(GovernorDeferred, match="request budget"):
        await g.acquire("s", deadline=c.t + 5)  # cannot start before the deadline
    t0 = c.t
    await g.acquire("s")  # without a deadline it waits for the window to roll
    assert c.t - t0 >= 60


async def test_budget_is_shared_across_governor_instances():
    c = Clock()
    a = gov(c, s=Budget(window_s=60, max_requests=2, floor=1))
    b = gov(c, s=Budget(window_s=60, max_requests=2, floor=1))  # "another process"
    await a.acquire("s")
    await b.acquire("s")
    with pytest.raises(GovernorDeferred):
        await a.acquire("s", deadline=c.t + 1)


async def test_crawler_gets_half_the_window():
    c = Clock()
    g = gov(c, s=Budget(window_s=60, max_requests=4, floor=1))
    await g.acquire("s", role="crawl")
    await g.acquire("s", role="crawl")
    with pytest.raises(GovernorDeferred):
        await g.acquire("s", role="crawl", deadline=c.t + 1)
    await g.acquire("s", role="search", deadline=c.t + 1)  # search still has headroom


async def test_cooldown_escalates_and_search_defers_immediately():
    c = Clock()
    g = gov(c)
    assert g.record_block("s") == COOLDOWN_BASE_S
    with pytest.raises(GovernorDeferred, match="cooling down"):
        await g.acquire("s")  # a search never waits out a cooldown
    c.t += COOLDOWN_BASE_S + 1
    assert g.record_block("s") == 2 * COOLDOWN_BASE_S  # second episode within memory


async def test_crawler_waits_out_cooldown_then_single_probe():
    c = Clock()
    g = gov(c)
    g.record_block("s")
    t0 = c.t
    await g.acquire("s", role="crawl", wait_cooldown=True)  # becomes the probe
    assert c.t - t0 >= COOLDOWN_BASE_S
    with pytest.raises(GovernorDeferred, match="probe"):
        await g.acquire("s", deadline=c.t + 1)  # others wait for the probe's verdict
    g.record_ok("s")
    await g.acquire("s", deadline=c.t + 1)


def test_learned_cap_from_burst_blocks_ignores_cold_blocks():
    t = 1_000_000.0
    burst = [(t + i * 2, "ok") for i in range(60)] + [(t + 121, "blocked")]
    cold = [(t + 5000, "ok"), (t + 5001, "blocked")]  # flagged profile, not volume
    cap = learn_cap_from_history(burst + cold, window_s=300, floor=6)
    assert cap == 36  # 60% of the 60 requests that preceded the burst block
    assert learn_cap_from_history(cold, window_s=300, floor=6) is None


async def test_learned_cap_tightens_but_never_below_floor(monkeypatch):
    c = Clock()
    g = Governor({"s": Budget(300, 100, floor=10)}, clock=c, sleep=c.sleep)
    g._learned["s"] = 4
    assert g.cap("s") == 10
    g._learned["s"] = 40
    assert g.cap("s") == 40 and g.cap("s", "crawl") == 20
