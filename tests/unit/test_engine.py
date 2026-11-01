"""Engine behaviour with fake sites: block handling, quarantine, empty-vs-failed."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime

import pytest

from jetbridge.acquire.adapters.base import Adapter, Capabilities
from jetbridge.acquire.engine import Engine
from jetbridge.acquire.scheduler import Scheduler
from jetbridge.acquire.types import ParseResult, Query, RawResponse


@pytest.fixture(autouse=True)
def _isolated_var(tmp_path, monkeypatch):
    monkeypatch.setenv("JETBRIDGE_VAR", str(tmp_path))
    from jetbridge.store import db

    db._engines.clear()


class FakeBroker:
    offscreen = True

    def __init__(self):
        self.rotations = 0

    async def page(self, site, warmup_url, settle_ms=0):
        return object()

    async def rotate(self, site):
        self.rotations += 1

    async def wait_for_human(self, site, timeout_s=0):
        return False


class Fake(Adapter):
    id = "fake"
    site = "fake"
    program = None
    warmup_url = "https://example.invalid"
    capabilities = Capabilities(award=True, cash=False)
    marker = '"ok":true'

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0

    async def fetch(self, page, q):
        self.calls += 1
        status, body = self.bodies.pop(0) if self.bodies else (403, "<title>Access Denied</title>")
        return RawResponse(
            status=status, body=body, tier="T1", started_at=datetime.now(UTC), latency_ms=1
        )

    def parse(self, raw, q, prov):
        return ParseResult()

    def is_empty_answer(self, raw):
        return '"flights":[]' in raw.body


def q(i):
    return Query("fake", "day", "CVG", "ICN", date(2027, 5, 20 + i), 3)


async def test_site_quarantined_after_consecutive_blocked_queries():
    fake = Fake([])
    br = FakeBroker()
    eng = Engine({"fake": fake}, br, Scheduler(fast=True), max_attempts=2)
    r1, r2 = await eng.run(q(1), use_cache=False), await eng.run(q(2), use_cache=False)
    assert r1.status == r2.status == "blocked"
    calls_before = fake.calls
    r3 = await eng.run(q(3), use_cache=False)
    assert r3.status == "blocked" and "--show-browser" in r3.reason
    assert fake.calls == calls_before  # no time wasted on a quarantined site
    assert br.rotations >= 2  # sessions were rotated while retrying


async def test_genuine_empty_vs_unrecognized_empty():
    fake = Fake([(200, '{"ok":true,"flights":[]}'), (200, '{"ok":true,"weird":1}')])
    eng = Engine({"fake": fake}, FakeBroker(), Scheduler(fast=True), max_attempts=1)
    assert (await eng.run(q(1), use_cache=False)).status == "empty"
    r = await eng.run(q(2), use_cache=False)
    assert r.status == "failed" and "not recognized as a genuine empty" in r.reason


async def test_block_page_with_200_is_not_empty():
    fake = Fake([(200, "<html><title>Challenge Validation</title>I'm not a robot</html>")])
    eng = Engine({"fake": fake}, FakeBroker(), Scheduler(fast=True), max_attempts=1)
    assert (await eng.run(q(1), use_cache=False)).status == "blocked"


async def test_acquire_defers_rows_past_deadline():
    import time as _t

    from sqlalchemy import select

    from jetbridge.runner import _acquire
    from jetbridge.store.db import LedgerRow, SearchRow, session

    with session() as s:
        s.add(SearchRow(id="s_t", spec={}, profile={}, status="running"))
        s.flush()
        for i in range(5):
            s.add(
                LedgerRow(
                    search_id="s_t",
                    stage="primary",
                    adapter="fake",
                    query_key=f"k{i}",
                    query={"query": q(i).to_dict(), "direction": "out", "priority": 1.0},
                    status="pending",
                )
            )
    with session() as s:
        rows = list(s.scalars(select(LedgerRow).where(LedgerRow.search_id == "s_t")))
    fake = Fake([(200, '{"ok":true,"flights":[]}')] * 5)
    eng = Engine({"fake": fake}, FakeBroker(), Scheduler(fast=True), max_attempts=1)
    await _acquire(eng, rows, deadline=_t.monotonic() - 1)  # already past
    with session() as s:
        sts = [r.status for r in s.scalars(select(LedgerRow).where(LedgerRow.search_id == "s_t"))]
        reasons = [
            r.reason for r in s.scalars(select(LedgerRow).where(LedgerRow.search_id == "s_t"))
        ]
    assert sts == ["deferred"] * 5 and fake.calls == 0
    assert all("--deep" in r for r in reasons)


async def test_hung_fetch_is_bounded_and_stage_cannot_stall():
    import time as _t

    class Hang(Fake):
        async def fetch(self, page, q):
            self.calls += 1
            await asyncio.sleep(3600)

    from jetbridge.acquire import engine as eng_mod

    hang = Hang([])
    hang.request_timeout_ms = 100  # bound = 0.1s + 30s grace normally; deadline tightens it
    eng = Engine(
        {"fake": hang},
        FakeBroker(),
        Scheduler(fast=True),
        max_attempts=1,
        deadline=_t.monotonic() - 25,
    )
    t = _t.monotonic()
    r = await eng.run(q(1), use_cache=False)
    assert r.status == "failed" and _t.monotonic() - t < 10  # bounded by deadline + grace
    assert eng_mod  # module import sanity
