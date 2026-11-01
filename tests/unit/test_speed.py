"""Speed mechanics: real concurrency, time-budgeted planning, metro de-duplication."""

from __future__ import annotations

import asyncio
import time

import pytest

from jetbridge.acquire.adapters import registry
from jetbridge.acquire.scheduler import Scheduler, SitePolicy
from jetbridge.kb import load_kb
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.plan.planner import build_plan, throughput_qps


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_VAR", str(tmp_path))  # no telemetry -> default latencies
    from jetbridge.store import db

    db._engines.clear()


async def test_scheduler_overlaps_concurrent_requests():
    sched = Scheduler(
        {
            "s": SitePolicy(
                min_interval_s=0.05, max_interval_s=0.05, concurrency=3, start_concurrency=3
            )
        }
    )

    async def req():
        async with sched.slot("s"):
            await asyncio.sleep(0.3)

    t = time.perf_counter()
    await asyncio.gather(*(req() for _ in range(6)))
    elapsed = time.perf_counter() - t
    assert elapsed < 1.0, elapsed  # serial would be >= 1.8s


def test_throughput_model():
    pol = SitePolicy(min_interval_s=1, max_interval_s=1, concurrency=4)
    assert throughput_qps(pol, 8.0) == pytest.approx(0.9 * 4 / 8)  # latency-bound
    assert throughput_qps(pol, 1.0) == pytest.approx(0.9 * 1.0)  # spacing-bound


def _spec(minutes: float) -> SearchSpec:
    return SearchSpec.model_validate(
        {
            "origins": ["CVG"],
            "destinations": [
                {"code": "HKG", "weight": 1.0},
                {"code": "ICN", "weight": 1.0},
                {"code": "ASIA_EAST", "weight": 0.8},
                {"code": "ASIA_SOUTHEAST", "weight": 0.6},
            ],
            "outbound": {"start": "2027-05-24", "end": "2027-06-08"},
            "passengers": 3,
            "time_budget_minutes": minutes,
        }
    )


def test_plan_defers_lowest_priority_first_and_never_primary_early():
    plan = build_plan(_spec(10), Profile(), load_kb(), registry())
    deferred = [p for p in plan.queries if p.skip_reason]
    kept = [p for p in plan.queries if not p.skip_reason]
    assert deferred and all(p.skip_reason.startswith("deferred") for p in deferred)
    for a in {p.query.adapter for p in deferred}:
        lo_kept = min(p.priority for p in kept if p.query.adapter == a)
        hi_def = max(p.priority for p in deferred if p.query.adapter == a)
        assert hi_def <= lo_kept  # strictly priority-ordered per site
        # primary destinations (priority >= 10) are never deferred while anything lower is kept
        assert not any(p.priority >= 10 for p in deferred if p.query.adapter == a) or lo_kept >= 10
    assert plan.est_seconds <= 10 * 60 * 1.05


def test_deep_plan_defers_nothing():
    plan = build_plan(_spec(10), Profile(), load_kb(), registry(), deep=True)
    assert not [p for p in plan.queries if p.skip_reason]


def test_united_searches_tokyo_once_per_day():
    plan = build_plan(_spec(600), Profile(), load_kb(), registry())
    ua = {p.query.destination for p in plan.queries if p.query.adapter == "united"}
    dl = {p.query.destination for p in plan.queries if p.query.adapter == "delta"}
    assert "HND" in ua and "NRT" not in ua  # united expands HND -> TYO (verified)
    assert {"HND", "NRT"} <= dl  # other engines are not assumed to


async def test_aimd_backs_off_on_blocks_and_recovers():
    sched = Scheduler(
        {
            "s": SitePolicy(
                min_interval_s=0,
                max_interval_s=0,
                concurrency=4,
                start_concurrency=4,
                increase_after=3,
            )
        }
    )
    assert sched.limit("s") == 4
    sched.report("s", blocked=True)
    assert sched.limit("s") == 2 and sched.snapshot()["s"]["spacing_mult"] == 2.0
    sched.report("s", blocked=True)
    assert sched.limit("s") == 1
    for _ in range(3):
        sched.report("s", blocked=False)
    assert sched.limit("s") == 2  # additive increase after a clean streak
    for _ in range(30):
        sched.report("s", blocked=False)
    assert sched.limit("s") == 4  # capped at policy max


async def test_adaptive_limit_is_enforced():
    sched = Scheduler(
        {"s": SitePolicy(min_interval_s=0, max_interval_s=0, concurrency=4, start_concurrency=2)}
    )
    peak = 0
    live = 0

    async def req():
        nonlocal peak, live
        async with sched.slot("s"):
            live += 1
            peak = max(peak, live)
            await asyncio.sleep(0.05)
            live -= 1

    await asyncio.gather(*(req() for _ in range(10)))
    assert peak == 2


def test_stage_deadlines_reserve_verification():
    from jetbridge.runner import (
        REPORT_RESERVE_S,
        SOLVE_RESERVE_S,
        STRAGGLER_GRACE_S,
        VERIFY_RESERVE_S,
        stage_deadlines,
    )

    for minutes in (5, 20, 60):
        a, r, v = stage_deadlines(0.0, minutes * 60)
        assert 0 < a <= r < v <= minutes * 60
        # refine stragglers (grace) + solve still leave the full verification slice
        assert v - (r + STRAGGLER_GRACE_S + SOLVE_RESERVE_S) >= VERIFY_RESERVE_S - STRAGGLER_GRACE_S
        assert minutes * 60 - v == REPORT_RESERVE_S
