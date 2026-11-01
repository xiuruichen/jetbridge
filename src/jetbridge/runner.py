"""Search orchestration — the agent loop.

    plan -> acquire (pass 1) -> build candidates -> refine:
        split completion + gateway long-haul (phase A) -> acquire
        positioning cash for gateways with space (phase B) -> acquire
    -> compose separate-ticket itineraries -> optimize (CP-SAT, k-best + restricted)
    -> advise (pre) -> live-verify the frontier -> advise (final) -> report

The database is the single source of truth: every observation is stored with
its search id, so resume / re-report / what-if rebuild exactly the same
candidate set without touching the network.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import select, update

from jetbridge.acquire.adapters import registry
from jetbridge.acquire.broker import SessionBroker
from jetbridge.acquire.engine import Engine, QueryResult, offers_from_rows
from jetbridge.acquire.types import ParseResult, Query
from jetbridge.kb import KB, load_kb
from jetbridge.models.core import AwardOffer, CashOffer
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.plan.planner import PlannedQuery, SearchPlan, build_plan
from jetbridge.plan.refine import compose, phase_a, phase_b
from jetbridge.solve.advisor import Brief, advise
from jetbridge.solve.allocator import Allocator, Solution
from jetbridge.solve.eligibility import Candidate, FilterStats, build
from jetbridge.solve.valuation import CashIndex
from jetbridge.store.db import (
    LedgerRow,
    ObservationRow,
    OptionRow,
    SearchRow,
    VerificationRow,
    session,
    utcnow,
)

log = logging.getLogger("jetbridge.runner")

SOLVER_TOP_PER_BUCKET = 40
VERIFY_TOP = 6
SOLVE_RESERVE_S = 60
VERIFY_RESERVE_S = 90
REPORT_RESERVE_S = 20
STRAGGLER_GRACE_S = 15


# --------------------------------------------------------------------------- create
def create_search(spec: SearchSpec, profile: Profile, **plan_kw) -> tuple[str, SearchPlan]:
    kb = load_kb()
    plan = build_plan(spec, profile, kb, registry(), **plan_kw)
    sid = f"s_{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}_{uuid.uuid4().hex[:6]}"
    with session() as s:
        s.add(
            SearchRow(
                id=sid,
                spec=spec.model_dump(mode="json"),
                profile=profile.model_dump(mode="json"),
                plan=plan.summary(),
                status="planned",
            )
        )
    add_ledger(sid, [(pq, pq.skip_reason) for pq in plan.queries], skipped=True)
    try:
        from jetbridge.crawl.watch import watch_from_search

        watch_from_search(sid, spec, profile)
    except Exception as exc:  # a watch is a convenience; never fail a search on it
        log.warning("could not register watch for %s: %r", sid, exc)
    return sid, plan


def add_ledger(
    sid: str, items: list[tuple[PlannedQuery, str | None]], *, skipped: bool = False
) -> list[LedgerRow]:
    """Insert planned queries (dedup by key). ``skipped`` marks rows that carry a
    skip reason; otherwise the reason is the *why* of a follow-up query."""
    rows: list[LedgerRow] = []
    with session() as s:
        existing = set(s.scalars(select(LedgerRow.query_key).where(LedgerRow.search_id == sid)))
        for pq, reason in items:
            key = pq.query.key + f":{pq.direction}"
            if key in existing:
                continue
            existing.add(key)
            is_skip = skipped and reason is not None
            status = "pending"
            if is_skip:
                status = "deferred" if reason.startswith("deferred") else "skipped"
            row = LedgerRow(
                search_id=sid,
                stage=pq.stage,
                adapter=pq.query.adapter,
                query_key=key,
                query={**pq.to_dict(), "why": None if is_skip else reason},
                status=status,
                reason=reason if is_skip else None,
            )
            s.add(row)
            rows.append(row)
    return rows


def _set(sid: str, **kw) -> None:
    with session() as s:
        s.execute(update(SearchRow).where(SearchRow.id == sid).values(updated_at=utcnow(), **kw))


def _load(sid: str) -> tuple[SearchSpec, Profile]:
    with session() as s:
        row = s.get(SearchRow, sid)
        assert row is not None, f"unknown search {sid}"
        return SearchSpec.model_validate(row.spec), Profile.model_validate(row.profile)


# --------------------------------------------------------------------------- collected
@dataclass
class Collected:
    results: list[tuple[str, QueryResult]] = field(default_factory=list)  # (direction, result)

    def offers(self) -> list[tuple[str, AwardOffer | CashOffer]]:
        return [(d, o) for d, r in self.results for o in [*r.parsed.awards, *r.parsed.cash]]


def collected_from_db(sid: str, spec: SearchSpec, kb: KB) -> Collected:
    with session() as s:
        obs = list(s.scalars(select(ObservationRow).where(ObservationRow.search_id == sid)))
    dests = _dests(spec, kb)
    col = Collected()
    for o in offers_from_rows(obs):
        direction = "in" if o.itinerary.origin in dests else "out"
        pr = ParseResult(
            awards=[o] if isinstance(o, AwardOffer) else [],
            cash=[o] if isinstance(o, CashOffer) else [],
        )
        col.results.append((direction, QueryResult(query=None, status="ok", parsed=pr)))  # type: ignore[arg-type]
    return col


def _origins(spec: SearchSpec, kb: KB) -> list[str]:
    out: list[str] = []
    for o in [*spec.origins, *spec.nearby_origins]:
        out += [a for a in kb.expand(o) if a not in out]
    return out


def _dests(spec: SearchSpec, kb: KB) -> dict[str, float]:
    dests: dict[str, float] = {}
    for d in spec.destinations:
        for ap in kb.expand(d.code):
            dests[ap] = max(dests.get(ap, 0), d.weight)
    return dests


# --------------------------------------------------------------------------- run
async def run_search(
    sid: str, *, offscreen: bool = True, retry_blocked: bool = False, deep: bool = False
) -> str:
    """Execute a planned search end to end (the full agent loop); returns report path.

    ``deep`` also runs queries deferred by the time budget."""
    import time as _time

    t_start = _time.monotonic()
    spec, profile = _load(sid)
    _set(sid, status="running", stage="acquire")
    kb = load_kb()
    adapters = registry()
    try:
        async with SessionBroker(offscreen=offscreen) as broker:
            from jetbridge.plan.planner import ACQUIRE_SHARE, REFINE_SHARE

            # Deadlines are computed BACKWARD from fixed end-of-run reserves so the
            # accuracy-critical live re-check always gets its slice:
            #   ... acquire | refine | solve (60s) | verify (90s) | report (20s)
            dl_acquire, dl_refine, dl_verify = (
                (None, None, None)
                if deep
                else stage_deadlines(t_start, spec.time_budget_minutes * 60)
            )
            _ = (ACQUIRE_SHARE, REFINE_SHARE)
            engine = Engine(
                adapters,
                broker,
                search_id=sid,
                logins_enabled=frozenset(profile.logins_enabled),
                deadline=dl_acquire,
            )
            await _acquire(engine, _pending(sid, retry_blocked, deep), dl_acquire)
            log.info(
                "acquisition done in %.0fs  rates=%s  engine=%s",
                _time.monotonic() - t_start,
                engine.scheduler.snapshot(),
                engine.stats,
            )
            if spec.refine and spec.refine_budget > 0 and not engine._past(30):
                engine.deadline = dl_refine
                await _refine(sid, spec, kb, adapters, engine, deep=deep, deadline=dl_refine)
            log.info("refinement done at %.0fs", _time.monotonic() - t_start)
            engine.deadline = dl_verify
            _set(sid, stage="solve")
            result = optimize(spec, profile, kb, collected_from_db(sid, spec, kb), adapters)
            _set(sid, stage="verify")
            ids = _option_ids(sid, result.solutions)
            pre = advise(result.solutions, ids, {}, spec, profile, kb, result.cash_index)
            targets = _verify_targets(pre, result.solutions)
            verified = await verify(engine, result.solutions, targets, deadline=dl_verify)
            log.info("verification done at %.0fs", _time.monotonic() - t_start)
        _set(sid, stage="report")
        return finalize(sid, spec, profile, kb, result, verified)
    except Exception as exc:
        log.exception("search %s failed", sid)
        _set(sid, status="failed", error=f"{type(exc).__name__}: {exc}"[:2000])
        raise


def stage_deadlines(t_start: float, budget_s: float) -> tuple[float, float, float]:
    """(acquire, refine, verify) deadlines, computed backward from fixed reserves so
    solve + live verification + report always fit inside the budget."""
    from jetbridge.plan.planner import ACQUIRE_SHARE

    end = t_start + budget_s
    dl_verify = end - REPORT_RESERVE_S
    dl_refine = dl_verify - VERIFY_RESERVE_S - SOLVE_RESERVE_S
    dl_acquire = min(t_start + budget_s * ACQUIRE_SHARE, dl_refine)
    return dl_acquire, dl_refine, dl_verify


def _cache_label(res: QueryResult) -> str:
    from jetbridge.store.db import FetchRow

    with session() as s:
        f = s.get(FetchRow, res.fetch_id) if res.fetch_id else None
    if f is None:
        return "served from the availability cache"
    t = f.started_at if f.started_at.tzinfo else f.started_at.replace(tzinfo=UTC)
    age_h = (datetime.now(UTC) - t).total_seconds() / 3600
    return f"served from the availability cache (observed {age_h:.1f} h ago)"


def _defer(row: LedgerRow, reason: str) -> None:
    with session() as s:
        s.execute(
            update(LedgerRow)
            .where(LedgerRow.id == row.id)
            .values(status="deferred", reason=reason, updated_at=utcnow())
        )


def _pending(sid: str, retry_blocked: bool = False, deep: bool = False) -> list[LedgerRow]:
    states = ["pending", "blocked", "failed"] if retry_blocked else ["pending"]
    if deep:
        states.append("deferred")
    with session() as s:
        return list(
            s.scalars(
                select(LedgerRow).where(LedgerRow.search_id == sid, LedgerRow.status.in_(states))
            )
        )


async def _refine(
    sid: str,
    spec: SearchSpec,
    kb: KB,
    adapters: dict,
    engine: Engine,
    *,
    deep: bool = False,
    deadline: float | None = None,
) -> None:
    from jetbridge.acquire.engine import DEFAULT_POLICIES
    from jetbridge.plan.planner import (
        DEFAULT_LATENCY_S,
        REFINE_SHARE,
        governed_qps,
        measured_latency_s,
        throughput_qps,
    )

    _set(sid, stage="refine")
    with session() as s:
        done = set(s.scalars(select(LedgerRow.query_key).where(LedgerRow.search_id == sid)))
    with session() as s:
        led = list(s.scalars(select(LedgerRow).where(LedgerRow.search_id == sid)))
    tried: dict[str, int] = defaultdict(int)
    blocked: dict[str, int] = defaultdict(int)
    for r in led:
        if r.status in ("ok", "empty", "blocked", "failed"):
            tried[r.adapter] += 1
            blocked[r.adapter] += r.status == "blocked"
    skip = {a for a, n in tried.items() if n >= 5 and blocked[a] / n > 0.5}
    skip |= {
        a.id
        for a in adapters.values()
        if engine.governor.cooldown_remaining(a.site) > 0 and not engine.human_present
    }
    if skip:
        log.info("refine: skipping sites that mostly blocked this search: %s", sorted(skip))
        adapters = {k: v for k, v in adapters.items() if k not in skip}
    budget = spec.refine_budget
    if not deep:
        # Follow-ups spread across the award sites: cap at REFINE_SHARE of the
        # clock at their aggregate measured throughput.
        agg = sum(
            min(
                throughput_qps(
                    DEFAULT_POLICIES.get(a.site),
                    measured_latency_s(a.id, DEFAULT_LATENCY_S.get(a.id, 6)),
                ),
                governed_qps(engine.governor, a.site),
            )
            for a in adapters.values()
            if a.capabilities.award
        )
        budget = min(budget, int(agg * spec.time_budget_minutes * 60 * REFINE_SHARE))
    col = collected_from_db(sid, spec, kb)
    cands, _stats, _gw, _pos = build_candidates(spec, kb, col)
    qa = phase_a(spec, kb, adapters, cands, _dests(spec, kb), done, int(budget * 0.75))
    rows = add_ledger(sid, [(r.pq, r.reason) for r in qa])
    log.info("refine phase A: %d follow-up queries", len(rows))
    await _acquire(engine, rows, deadline)
    col = collected_from_db(sid, spec, kb)
    through_cands, _s, gateway_offers, _p = build_candidates(spec, kb, col)
    with session() as s:
        done = set(s.scalars(select(LedgerRow.query_key).where(LedgerRow.search_id == sid)))
    qb = phase_b(spec, kb, adapters, gateway_offers, done, budget - len(rows), through_cands)
    rows_b = add_ledger(sid, [(r.pq, r.reason) for r in qb])
    log.info("refine phase B: %d positioning queries", len(rows_b))
    await _acquire(engine, rows_b, deadline)


async def _acquire(engine: Engine, ledger: list[LedgerRow], deadline: float | None = None) -> None:
    """Run ledger rows: per site, ``max concurrency`` workers pull from a priority
    queue (the adaptive scheduler gates how many are actually in flight). At the
    deadline, rows not yet started are marked DEFERRED with a reason."""
    import time as _time

    by_adapter: dict[str, list[LedgerRow]] = defaultdict(list)
    for row in sorted(ledger, key=lambda r: -r.query["priority"]):
        by_adapter[row.adapter].append(row)

    async def worker(queue: asyncio.Queue) -> None:
        while not queue.empty():
            row = queue.get_nowait()
            q = Query.from_dict(row.query["query"])
            if deadline is not None and _time.monotonic() > deadline:
                hit = engine.lookup_cache(q)  # a fresh stored answer costs nothing
                if hit is None:
                    _defer(
                        row,
                        "deferred: time budget reached before this query ran; the background "
                        "crawler will fill it (or `jetbridge resume <search_id> --deep`)",
                    )
                    continue
                res = hit
            else:
                res = await engine.run(q)
            if res.from_cache:
                res.reason = res.reason or _cache_label(res)
            with session() as s:
                s.execute(
                    update(LedgerRow)
                    .where(LedgerRow.id == row.id)
                    .values(
                        status=res.status,
                        reason=(res.reason or ("; ".join(res.parsed.warnings[:3]) or None)),
                        offers=len(res.parsed.awards) + len(res.parsed.cash),
                        fetch_id=res.fetch_id,
                        attempts=res.attempts,
                        updated_at=utcnow(),
                    )
                )

    tasks = []
    for adapter_id, rows in by_adapter.items():
        queue: asyncio.Queue = asyncio.Queue()
        for r in rows:  # already in priority order
            queue.put_nowait(r)
        site = engine.adapters[adapter_id].site
        n = max(1, engine.scheduler.policy(site).concurrency)
        tasks += [worker(queue) for _ in range(n)]
    if not tasks:
        return
    futs = [asyncio.ensure_future(t) for t in tasks]
    timeout = (
        None if deadline is None else max(1.0, deadline - _time.monotonic() + STRAGGLER_GRACE_S)
    )
    _done, stragglers = await asyncio.wait(futs, timeout=timeout)
    for f in stragglers:  # in flight 30s past the deadline: cancel, never stall the run
        f.cancel()
    if stragglers:
        await asyncio.wait(stragglers, timeout=10)
        with session() as s:
            ids = [r.id for r in ledger]
            s.execute(
                update(LedgerRow)
                .where(LedgerRow.id.in_(ids), LedgerRow.status == "pending")
                .values(
                    status="deferred",
                    reason="deferred: still in flight when the time budget ended; "
                    "run `jetbridge resume <search_id> --deep` to include",
                    updated_at=utcnow(),
                )
            )


# --------------------------------------------------------------------------- candidates
def build_candidates(
    spec: SearchSpec, kb: KB, collected: Collected
) -> tuple[list[Candidate], FilterStats, list[tuple[str, AwardOffer]], list[CashOffer]]:
    """Hard-filter + analyze every observed offer; compose positioning itineraries.

    Returns (candidates, stats, gateway_awards, positioning_cash)."""
    origins = _origins(spec, kb)
    home = set(origins)
    dests = _dests(spec, kb)
    gateways = {g for p in kb.programs.values() for g in p.gateways} | set(kb.gateways)
    stats = FilterStats()
    best: dict[tuple, Candidate] = {}
    gateway_awards: dict[str, tuple[str, AwardOffer]] = {}
    positioning: dict[str, CashOffer] = {}

    def keep(cand: Candidate) -> None:
        o = cand.offer
        prog = o.program if isinstance(o, AwardOffer) else f"cash:{o.source}"
        key = (cand.direction, prog, cand.group, o.itinerary.main_cabin, o.provenance.request_pax)
        prev = best.get(key)
        if prev is None or _cost_key(cand) < _cost_key(prev):
            best[key] = cand

    for direction, o in collected.offers():
        stats.seen += 1
        it = o.itinerary
        # classify building blocks for composition
        if isinstance(o, AwardOffer):
            if (
                direction == "out"
                and it.origin in gateways
                and it.origin not in home
                and it.destination in dests
            ):
                gateway_awards[o.offer_id + ":out"] = ("out", o)
            elif (
                direction == "in"
                and it.destination in gateways
                and it.destination not in home
                and it.origin in dests
            ):
                gateway_awards[o.offer_id + ":in"] = ("in", o)
        elif (it.origin in home and it.destination in gateways and it.destination not in dests) or (
            it.origin in gateways and it.destination in home
        ):
            positioning[o.offer_id] = o
        cand, why = build(o, direction, spec, origins, dests, kb)
        if cand is None:
            stats.reject(why or "?")
            continue
        keep(cand)

    for direction, aw, before, after in compose(
        spec, kb, list(gateway_awards.values()), list(positioning.values())
    ):
        cand, why = build(
            aw, direction, spec, origins, dests, kb, extras=before, extras_after=after
        )
        if cand is None:
            stats.reject(f"composed: {why}")
            continue
        keep(cand)

    cands = list(best.values())
    stats.kept = len(cands)
    return cands, stats, list(gateway_awards.values()), list(positioning.values())


def _cost_key(c: Candidate) -> tuple:
    o = c.offer
    price = Decimal(o.miles) if isinstance(o, AwardOffer) else o.price.amount
    return (price, c.cash_per_pax, c.penalty_usd, -o.provenance.observed_at.timestamp())


# --------------------------------------------------------------------------- optimize
@dataclass
class OptimizeResult:
    solutions: list[Solution]
    candidates: list[Candidate]
    stats: FilterStats
    cash_index: CashIndex


def optimize(
    spec: SearchSpec, profile: Profile, kb: KB, collected: Collected, adapters: dict
) -> OptimizeResult:
    cands, stats, _gw, _pos = build_candidates(spec, kb, collected)
    cash_index = CashIndex(cands)
    allocator = Allocator(
        spec, profile, kb, {a.id: a.price_independent_of_pax for a in adapters.values()}
    )
    # Bound solver size: cheapest-by-utility N per (direction, destination, cabin, pax-class, composed).
    buckets: dict[tuple, list[Candidate]] = defaultdict(list)
    for c in cands:
        it = c.itinerary
        end = it.destination if c.direction == "out" else it.origin
        buckets[
            (
                c.direction,
                end,
                c.offer.itinerary.main_cabin,
                c.offer.provenance.request_pax,
                c.composed,
            )
        ].append(c)
    pool: list[Candidate] = []
    for group in buckets.values():
        group.sort(key=lambda c: _rough_cost(c, allocator))
        pool += group[:SOLVER_TOP_PER_BUCKET]

    sols: dict[tuple, Solution] = {}

    def add(s: Solution, label: str = "") -> None:
        if s.key() not in sols:
            s.label = label
            sols[s.key()] = s
        elif label and not sols[s.key()].label:
            sols[s.key()].label = label

    for s in allocator.solve(pool, k=30, time_limit_s=5, deadline_s=45):
        add(s)
    for cabin in sorted({c.offer.itinerary.main_cabin for c in pool}, reverse=True):
        for i, s in enumerate(
            allocator.solve(pool, k=2, time_limit_s=4, deadline_s=6, restrict={"cabin": cabin})
        ):
            add(s, f"best {cabin.name.lower()}" if i == 0 else "")
    for prog in sorted({c.offer.program for c in pool if isinstance(c.offer, AwardOffer)}):
        for s in allocator.solve(pool, k=1, time_limit_s=4, restrict={"program": prog}):
            add(s, f"best using {prog}")
    if any(c.composed for c in pool):
        for s in allocator.solve(
            pool, k=2, time_limit_s=4, deadline_s=6, restrict={"composed": True}
        ):
            add(s, "best with positioning")
    ordered = _diversify(sorted(sols.values(), key=lambda s: s.objective_cents))
    if ordered:
        ordered[0].label = "best overall"
    return OptimizeResult(ordered[:30], cands, stats, cash_index)


def solve(
    spec: SearchSpec, profile: Profile, kb: KB, collected: Collected, adapters: dict
) -> tuple[list[Solution], list[Candidate], FilterStats]:
    """Back-compat wrapper used by tests and older call sites."""
    r = optimize(spec, profile, kb, collected, adapters)
    return r.solutions, r.candidates, r.stats


def _signature(s: Solution) -> tuple:
    return tuple(
        sorted(
            (
                a.candidate.direction,
                a.candidate.offer.program if isinstance(a.candidate.offer, AwardOffer) else "cash",
                a.candidate.offer.itinerary.main_cabin.value,
                a.candidate.itinerary.destination,
                a.candidate.itinerary.departs_at.date().isoformat(),
                a.candidate.composed,
            )
            for a in s.assignments
        )
    )


def _diversify(ordered: list[Solution], per_signature: int = 2) -> list[Solution]:
    seen: dict[tuple, int] = defaultdict(int)
    out = []
    for s in ordered:
        sig = _signature(s)
        if s.label or seen[sig] < per_signature:
            out.append(s)
            seen[sig] += 1
    return out


def _rough_cost(c: Candidate, alloc: Allocator) -> float:
    o = c.offer
    cents = float(c.cash_per_pax) * 100
    if isinstance(o, AwardOffer):
        cents += float(o.miles) * float(alloc.cpp(alloc.kb.programs[o.program].currency))
    return cents + c.penalty_usd * 100


# --------------------------------------------------------------------------- verify
@dataclass
class Verdict:
    status: str  # confirmed|changed|gone|unverifiable
    detail: dict


def _verify_targets(brief: Brief, sols: list[Solution]) -> list[int]:
    idx: list[int] = []
    for o in [brief.recommendation, *[a for _, a, _ in brief.alternatives], *brief.frontier]:
        if o is not None and o.index not in idx:
            idx.append(o.index)
    for i in range(len(sols)):
        if len(idx) >= VERIFY_TOP:
            break
        if i not in idx:
            idx.append(i)
    return idx[:VERIFY_TOP]


async def verify(
    engine: Engine,
    sols: list[Solution],
    targets: list[int] | None = None,
    *,
    deadline: float | None = None,
) -> dict[int, dict[str, Verdict]]:
    """Live re-check of selected options. Returns {solution index: {offer_id: verdict}}."""
    out: dict[int, dict[str, Verdict]] = {}
    idx = list(targets if targets is not None else range(min(VERIFY_TOP, len(sols))))
    # Re-fetch every distinct query first, concurrently (sites run in parallel).
    wanted: dict[str, Query] = {}
    for i in idx:
        for a in sols[i].assignments:
            o = a.candidate.offer
            if isinstance(o, AwardOffer):
                it = o.itinerary
                q = Query(
                    o.provenance.adapter,
                    "day",
                    it.origin,
                    it.destination,
                    it.departs_at.date(),
                    o.provenance.request_pax,
                )
                wanted[q.key] = q
    import time as _time

    tasks = {k: asyncio.ensure_future(engine.run(q, use_cache=False)) for k, q in wanted.items()}
    timeout = None if deadline is None else max(5.0, deadline - _time.monotonic())
    if tasks:
        await asyncio.wait(tasks.values(), timeout=timeout)
    fresh: dict[str, QueryResult] = {}
    for k, t in tasks.items():
        if t.done() and not t.cancelled() and t.exception() is None:
            fresh[k] = t.result()
        else:
            t.cancel()
            fresh[k] = QueryResult(wanted[k], "failed", reason="re-check skipped: time budget")
    for i in idx:
        sol = sols[i]
        out[i] = {}
        for a in sol.assignments:
            for x in a.candidate.positioning:
                out[i][x.offer_id] = Verdict(
                    "unverifiable", {"why": "cash positioning not re-checked"}
                )
            o = a.candidate.offer
            if not isinstance(o, AwardOffer):
                out[i][o.offer_id] = Verdict("unverifiable", {"why": "cash offers not re-checked"})
                continue
            it = o.itinerary
            q = Query(
                o.provenance.adapter,
                "day",
                it.origin,
                it.destination,
                it.departs_at.date(),
                o.provenance.request_pax,
            )
            if q.key not in fresh:
                fresh[q.key] = await engine.run(q, use_cache=False)
            out[i][o.offer_id] = _judge(o, a.pax, fresh[q.key])
            with session() as s:
                s.add(
                    VerificationRow(
                        option_id=f"{i}",
                        offer_id=o.offer_id,
                        status=out[i][o.offer_id].status,
                        detail=out[i][o.offer_id].detail,
                    )
                )
    return out


def _judge(o: AwardOffer, pax: int, res: QueryResult) -> Verdict:
    if res.status not in ("ok", "empty"):
        return Verdict("unverifiable", {"why": res.status, "reason": res.reason})
    same = [
        n
        for n in res.parsed.awards
        if n.itinerary.flight_key == o.itinerary.flight_key
        and n.itinerary.main_cabin == o.itinerary.main_cabin
        and n.program == o.program
    ]
    if not same:
        return Verdict(
            "gone", {"rechecked_at": datetime.now(UTC).isoformat(), "fetch_id": res.fetch_id}
        )
    n = min(same, key=lambda x: x.miles)
    det = {
        "rechecked_at": n.provenance.observed_at.isoformat(),
        "fetch_id": n.provenance.fetch_id,
        "miles_before": o.miles,
        "miles_now": n.miles,
        "taxes_before": str(o.taxes.amount),
        "taxes_now": str(n.taxes.amount),
        "seats_now": f"{n.seats.kind}:{n.seats.count}",
    }
    seats_ok = n.seats.count >= pax
    if n.miles == o.miles and n.taxes.amount == o.taxes.amount and seats_ok:
        return Verdict("confirmed", det)
    return Verdict("changed" if seats_ok else "gone", det)


def latest_verdicts(sols: list[Solution]) -> dict[int, dict[str, Verdict]]:
    """Most recent stored verdict per offer, mapped onto solution indices."""
    wanted = {o.offer_id for s_ in sols for a in s_.assignments for o in a.candidate.offers}
    latest: dict[str, Verdict] = {}
    if wanted:
        with session() as s:
            for v in s.scalars(
                select(VerificationRow)
                .where(VerificationRow.offer_id.in_(wanted))
                .order_by(VerificationRow.checked_at)
            ):
                latest[v.offer_id] = Verdict(v.status, v.detail)
    return {
        i: {
            o.offer_id: latest[o.offer_id]
            for a in s_.assignments
            for o in a.candidate.offers
            if o.offer_id in latest
        }
        for i, s_ in enumerate(sols)
    }


# --------------------------------------------------------------------------- finalize
def _option_ids(sid: str, sols: list[Solution]) -> list[str]:
    return [f"opt_{sid[-6:]}_{i + 1:02d}" for i in range(len(sols))]


def finalize(
    sid: str,
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    result: OptimizeResult,
    verified: dict[int, dict[str, Verdict]],
) -> str:
    from jetbridge.report.render import write_report

    ids = _option_ids(sid, result.solutions)
    brief = advise(result.solutions, ids, verified, spec, profile, kb, result.cash_index)
    _store_options(sid, result.solutions, verified, ids, brief)
    path = write_report(
        sid,
        spec,
        profile,
        kb,
        result.solutions,
        verified,
        ids,
        result.candidates,
        result.stats,
        brief,
    )
    _set(sid, status="done", stage=None, report_path=str(path))
    return str(path)


def _store_options(
    sid: str,
    sols: list[Solution],
    verified: dict[int, dict[str, Verdict]],
    ids: list[str] | None = None,
    brief: Brief | None = None,
) -> list[str]:
    ids = ids or _option_ids(sid, sols)
    analysis = {o.option_id: o for o in brief.all} if brief else {}
    with session() as s:
        for old in s.scalars(select(OptionRow).where(OptionRow.search_id == sid)):
            s.delete(old)
        s.flush()
        for i, sol in enumerate(sols):
            oid = ids[i]
            v = verified.get(i, {})
            an = analysis.get(oid)
            s.add(
                OptionRow(
                    id=oid,
                    search_id=sid,
                    rank=i + 1,
                    status=_option_status(v),
                    data={
                        "label": sol.label,
                        "objective_cents": sol.objective_cents,
                        "value_cents": sol.value_cents,
                        "miles_by_program": sol.miles_by_program,
                        "cash_usd": str(sol.cash_usd),
                        "transfers": [t.__dict__ for t in sol.transfers],
                        "own_used": sol.own_used,
                        "analysis": None
                        if an is None
                        else {
                            "utility_usd": round(an.utility_usd, 0),
                            "points_value_usd": round(an.points_usd, 2),
                            "hours": round(an.hours, 1),
                            "comfort_deficit_usd": round(an.comfort_usd, 0),
                            "risk": round(an.risk, 3),
                            "risk_reasons": an.risk_reasons,
                            "dominated_by": an.dominated_by,
                            "redemptions": an.redemptions,
                        },
                        "assignments": [
                            {
                                "pax": a.pax,
                                "direction": a.candidate.direction,
                                "flags": a.candidate.flags,
                                "offer": a.candidate.offer.model_dump(mode="json"),
                                "positioning": [
                                    x.model_dump(mode="json") for x in a.candidate.positioning
                                ],
                                "risk": round(a.candidate.risk, 3),
                                "risk_reasons": a.candidate.risk_reasons,
                                "quality": _quality_dict(a.candidate),
                                "verification": v[a.candidate.offer.offer_id].__dict__
                                if a.candidate.offer.offer_id in v
                                else None,
                            }
                            for a in sol.assignments
                        ],
                    },
                )
            )
    return ids


def _quality_dict(c: Candidate) -> dict | None:
    q = c.quality
    if q is None:
        return None
    return {
        "door_to_door_hours": round(q.total_hours, 2),
        "flown_hours": round(q.flown_hours, 2),
        "hours_by_cabin": {k.name.lower(): round(v, 2) for k, v in q.hours_by_cabin.items()},
        "longhaul": {"cabin": q.longhaul_cabin.name.lower(), "hours": round(q.longhaul_hours, 2)},
        "comfort_deficit_usd_per_pax": round(q.comfort_deficit_usd, 0),
        "redeye": q.redeye,
        "layovers": [
            {
                "airport": lv.airport,
                "minutes": lv.minutes,
                "overnight": lv.overnight,
                "tight": lv.tight,
                "airport_change": list(lv.airport_change) if lv.airport_change else None,
                "separate_ticket": lv.separate_ticket,
            }
            for lv in q.layovers
        ],
        "penalty_usd_per_pax": round(c.penalty_usd, 0),
    }


def _option_status(v: dict[str, Verdict]) -> str:
    if not v:
        return "unverified"
    sts = {x.status for x in v.values()}
    for s in ("gone", "changed"):
        if s in sts:
            return s
    if sts == {"confirmed"}:
        return "confirmed"
    return "partially_verified"


# --------------------------------------------------------------------------- offline tools
def rereport(
    sid: str, profile: Profile | None = None, spec_override: SearchSpec | None = None
) -> str:
    """Re-solve + re-advise + re-render from stored observations (no network)."""
    spec, prof = _load(sid)
    spec = spec_override or spec
    prof = profile or prof
    kb = load_kb()
    result = optimize(spec, prof, kb, collected_from_db(sid, spec, kb), registry())
    return finalize(sid, spec, prof, kb, result, latest_verdicts(result.solutions))


def what_if(sid: str, profile: Profile | None = None, spec: SearchSpec | None = None) -> dict:
    """Re-optimize a finished search under different balances/valuations/preferences
    WITHOUT overwriting it. Returns the decision brief."""
    base_spec, base_prof = _load(sid)
    spec = spec or base_spec
    prof = profile or base_prof
    kb = load_kb()
    result = optimize(spec, prof, kb, collected_from_db(sid, base_spec, kb), registry())
    ids = [f"whatif_{i + 1:02d}" for i in range(len(result.solutions))]
    brief = advise(
        result.solutions, ids, latest_verdicts(result.solutions), spec, prof, kb, result.cash_index
    )
    return brief.to_dict()


def brief_for(sid: str) -> Brief:
    spec, prof = _load(sid)
    kb = load_kb()
    result = optimize(spec, prof, kb, collected_from_db(sid, spec, kb), registry())
    ids = _option_ids(sid, result.solutions)
    return advise(
        result.solutions, ids, latest_verdicts(result.solutions), spec, prof, kb, result.cash_index
    )


async def targeted(sid: str, queries: list[Query], why: str, *, offscreen: bool = True) -> dict:
    """Agent-initiated follow-up searches folded into an existing search, then re-solved."""
    spec, profile = _load(sid)
    rows = add_ledger(
        sid,
        [
            (
                PlannedQuery(
                    q, "agent", "in" if q.origin in _dests(spec, load_kb()) else "out", 5.0
                ),
                why,
            )
            for q in queries
        ],
    )
    adapters = registry()
    async with SessionBroker(offscreen=offscreen) as broker:
        engine = Engine(
            adapters, broker, search_id=sid, logins_enabled=frozenset(profile.logins_enabled)
        )
        await _acquire(engine, rows)
    with session() as s:
        res = [
            {"query": r.query["query"], "status": r.status, "offers": r.offers, "reason": r.reason}
            for r in s.scalars(select(LedgerRow).where(LedgerRow.id.in_([x.id for x in rows])))
        ]
    path = rereport(sid)
    return {"ran": res, "report": path}


def ledger_stats(sid: str) -> dict:
    with session() as s:
        rows = list(s.scalars(select(LedgerRow).where(LedgerRow.search_id == sid)))
    by: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for r in rows:
        by[r.adapter][r.status] += 1
    return {a: dict(v) for a, v in by.items()}


def reparse(sid: str) -> dict[str, int]:
    """Re-run the CURRENT parsers over every stored raw response of a search (no
    network), replacing its observations and updating ledger statuses. Used after
    a parser fix: recovers data that an older parser dropped."""
    from sqlalchemy import delete

    from jetbridge.acquire.cache import get_blob
    from jetbridge.acquire.types import RawResponse
    from jetbridge.store.db import FetchRow

    adapters = registry()
    engine = Engine(adapters, broker=None, search_id=sid)
    changed: dict[str, int] = defaultdict(int)
    with session() as s:
        rows = list(
            s.scalars(
                select(LedgerRow).where(LedgerRow.search_id == sid, LedgerRow.fetch_id.is_not(None))
            )
        )
        fetches = {
            f.id: f
            for f in s.scalars(select(FetchRow).where(FetchRow.id.in_([r.fetch_id for r in rows])))
        }
    for row in rows:
        f = fetches.get(row.fetch_id)
        if f is None or not f.blob or f.outcome not in ("ok", "empty"):
            continue
        q = Query.from_dict(row.query["query"])
        raw = RawResponse(
            status=f.http_status,
            body=get_blob(f.blob),
            tier=f.tier,  # type: ignore[arg-type]
            started_at=f.started_at if f.started_at.tzinfo else f.started_at.replace(tzinfo=UTC),
            latency_ms=f.latency_ms,
        )
        with session() as s:
            s.execute(
                delete(ObservationRow).where(
                    ObservationRow.search_id == sid, ObservationRow.fetch_id == f.id
                )
            )
        res = engine._interpret(adapters[q.adapter], q, raw, f.id)
        n = len(res.parsed.awards) + len(res.parsed.cash)
        if res.status != row.status or n != row.offers:
            changed[f"{row.status}->{res.status}"] += 1
        with session() as s:
            s.execute(
                update(LedgerRow)
                .where(LedgerRow.id == row.id)
                .values(
                    status=res.status,
                    offers=n,
                    reason=res.reason or ("; ".join(res.parsed.warnings[:3]) or None),
                    updated_at=utcnow(),
                )
            )
    return dict(changed)
