"""SearchSpec -> SearchPlan, under a wall-clock budget.

Principles
----------
* **Real party size.** Award prices for AA/United change with party size, so
  base searches use the actual passenger count (authoritative for the party).
  Split-booking probes are evidence-driven follow-ups (``plan/refine.py``), not
  a blanket second pass.
* **Time budget.** Each site's throughput is estimated from *measured* latency
  telemetry (recent fetches in the DB) and its concurrency/spacing policy. Sites
  run in parallel, so each gets the acquisition share of the budget. Queries
  are filled in priority order; the remainder is DEFERRED — written to the
  ledger with a reason and resumable via ``jetbridge resume <id> --deep`` —
  never silently dropped.
* **Priority** = destination preference weight x hub factor (airports with
  nonstop service from North America rank higher, since itineraries elsewhere
  usually connect through them) x home-airport factor; days in order.
* **No redundant queries.** Engines that expand an airport to its metro
  (verified: united.com HND -> TYO) are searched once per metro.
* **Cache first (seats.aero model).** A query whose stored answer is still
  fresh for its departure horizon (``freshness.ttl_for``; party-size dominance
  for Delta/Alaska) is served from the store and costs no live budget. The
  background crawler keeps watched routes warm, so a repeat or overlapping
  search is mostly instant.
* **Governed capacity.** Live capacity per site is the lower of measured
  throughput and the rate governor's sliding-window budget; a site cooling
  down after a block contributes nothing until its cooldown ends.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from sqlalchemy import select

from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.cache import fresh_index
from jetbridge.acquire.engine import DEFAULT_POLICIES
from jetbridge.acquire.freshness import serving_queries, ttl_for
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.scheduler import SitePolicy
from jetbridge.acquire.types import Query
from jetbridge.kb import KB
from jetbridge.models.spec import Profile, SearchSpec

# Fallback median latencies (s) when no telemetry exists yet (from recon runs).
DEFAULT_LATENCY_S = {
    "united": 5.6,
    "american": 7.2,
    "delta": 3.6,
    "alaska": 1.9,
    "google_flights": 9.0,
}
ACQUIRE_SHARE = 0.62  # of the time budget; refine gets the rest minus ~170s of reserves
REFINE_SHARE = 0.15


@dataclass
class PlannedQuery:
    query: Query
    stage: str  # primary|cash|refine-*|agent
    direction: str  # out|in
    priority: float
    skip_reason: str | None = None
    cached: bool = False  # a fresh stored answer exists: no live request needed

    def to_dict(self) -> dict:
        return {
            "query": self.query.to_dict(),
            "stage": self.stage,
            "direction": self.direction,
            "priority": self.priority,
            "skip_reason": self.skip_reason,
            "cached": self.cached,
        }


@dataclass
class SearchPlan:
    queries: list[PlannedQuery]
    origins: list[str]
    destinations: dict[str, float]
    adapters: list[str]
    excluded_programs: dict[str, str]
    est_seconds: float
    throughput: dict[str, float] = field(default_factory=dict)  # queries/s per adapter
    notes: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> list[PlannedQuery]:
        return [q for q in self.queries if q.skip_reason is None]

    def summary(self) -> dict:
        by_adapter: dict[str, int] = {}
        deferred: dict[str, int] = {}
        cached: dict[str, int] = {}
        for pq in self.queries:
            tgt = by_adapter if pq.skip_reason is None else deferred
            if pq.skip_reason is None and pq.cached:
                tgt = cached
            tgt[pq.query.adapter] = tgt.get(pq.query.adapter, 0) + 1
        return {
            "origins": self.origins,
            "destinations": self.destinations,
            "adapters": self.adapters,
            "excluded_programs": self.excluded_programs,
            "queries_total": len(self.queries),
            "queries_runnable": len(self.runnable),
            "queries_deferred": len(self.queries) - len(self.runnable),
            "queries_cached": sum(cached.values()),
            "live_by_adapter": by_adapter,
            "cached_by_adapter": cached,
            "deferred_by_adapter": deferred,
            "throughput_qps": {k: round(v, 2) for k, v in self.throughput.items()},
            "estimated_minutes": round(self.est_seconds / 60, 1),
            "notes": self.notes,
        }

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "queries": [q.to_dict() for q in self.queries]}


# --------------------------------------------------------------------- throughput
def measured_latency_s(adapter: str, default: float) -> float:
    """Median latency of recent successful live fetches (telemetry), else ``default``."""
    try:
        from jetbridge.store.db import FetchRow, session

        with session() as s:
            rows = list(
                s.scalars(
                    select(FetchRow.latency_ms)
                    .where(
                        FetchRow.adapter == adapter,
                        FetchRow.outcome == "ok",
                        FetchRow.tier != "replay",
                    )
                    .order_by(FetchRow.started_at.desc())
                    .limit(200)
                )
            )
        if len(rows) >= 5:
            return statistics.median(rows) / 1000
    except Exception:
        pass
    return default


def governed_qps(governor: Governor | None, site: str) -> float:
    """Sustainable q/s under the rate governor's window budget (inf if unbudgeted)."""
    if governor is None:
        return float("inf")
    b = governor.budgets.get(site)
    cap = governor.cap(site)
    if b is None or cap is None:
        return float("inf")
    return cap / b.window_s


def throughput_qps(policy: SitePolicy | None, latency_s: float) -> float:
    """Sustained queries/s = min(concurrency / latency, 1 / mean start spacing), -10%."""
    pol = policy or SitePolicy()
    gap = (pol.min_interval_s + pol.max_interval_s) / 2
    return min(pol.concurrency / max(latency_s, 0.3), 1 / max(gap, 0.05)) * 0.9


# --------------------------------------------------------------------- helpers
def _reachable(program: str, profile: Profile, kb: KB) -> tuple[bool, str]:
    if not profile.balances:
        return True, "no balances in profile: all programs searched"
    for src in kb.funding_sources(program):
        if profile.balances.get(src.source, 0) > 0:
            return True, f"funded by {src.source}"
    return False, "no balance in this program or any card currency that transfers to it"


def _metro_members(kb: KB, adapter_id: str, airport: str) -> list[str] | None:
    """Members of ``airport``'s metro if ``adapter_id``'s engine expands that metro."""
    for metro in kb.metro_expanding_adapters.get(adapter_id, []):
        members = kb.metros.get(metro, [])
        if airport in members:
            return members
    return None


def build_plan(
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    adapters: dict[str, Adapter],
    *,
    max_queries_per_adapter: int | None = None,
    deep: bool = False,
    governor: Governor | None = None,
    use_cache: bool = True,
) -> SearchPlan:
    notes: list[str] = []
    if governor is None:
        from jetbridge.acquire.engine import site_adapters

        governor = Governor(site_adapters=site_adapters(adapters))
    excluded: dict[str, str] = {}

    # ---- which award adapters ----------------------------------------------
    chosen: list[str] = []
    for pid in spec.programs or list(kb.programs):
        prog = kb.programs.get(pid)
        if prog is None:
            excluded[pid] = "unknown program"
            continue
        if prog.adapter not in adapters:
            excluded[pid] = "adapter not implemented yet"
            continue
        ok, why = _reachable(pid, profile, kb)
        if not ok:
            excluded[pid] = why
            continue
        a = adapters[prog.adapter]
        if a.capabilities.needs_login and a.id not in profile.logins_enabled:
            excluded[pid] = "requires sign-in (enable in profile.logins_enabled)"
            continue
        chosen.append(a.id)

    # ---- geography ------------------------------------------------------------
    origins: list[str] = []
    for o in [*spec.origins, *spec.nearby_origins]:
        origins += [ap for ap in kb.expand(o) if ap not in origins]
    dests: dict[str, float] = {}
    for d in spec.destinations:
        for ap in kb.expand(d.code):
            dests[ap] = max(dests.get(ap, 0.0), d.weight)
    for ap in list(dests):
        if ap in origins:
            del dests[ap]
    hubs = set(kb.na_nonstop_asia)

    def dest_priority(w: float, hub: bool) -> float:
        if w >= 0.9:
            return 10 + w  # primary destinations always first
        return w * (1.0 if hub else 0.6)

    directions = [("out", spec.outbound.days())]
    if spec.inbound:
        directions.append(("in", spec.inbound.days()))

    pqs: list[PlannedQuery] = []
    metro_skips = 0
    for direction, days in directions:
        for a in chosen:
            for dest, w in dests.items():
                members = _metro_members(kb, a, dest)
                if members:
                    rep = next(m for m in members if m in dests)
                    if rep != dest:
                        metro_skips += len(days) * len(origins)
                        continue  # covered by the representative airport's search
                    w = max(dests[m] for m in members if m in dests)
                    hub = any(m in hubs for m in members)
                else:
                    hub = dest in hubs
                dp = dest_priority(w, hub)
                for org in origins:
                    o, d = (org, dest) if direction == "out" else (dest, org)
                    home = 1.0 if org == spec.origins[0] else 0.8
                    for i, day in enumerate(days):
                        pqs.append(
                            PlannedQuery(
                                Query(a, "day", o, d, day, spec.passengers),
                                "primary",
                                direction,
                                priority=dp * home - i * 1e-4,
                            )
                        )
    if metro_skips:
        notes.append(
            f"{metro_skips} queries avoided: the airline's search already covers the whole "
            "metro (e.g. HND+NRT)"
        )

    # ---- cash fares: primary destinations, preferred + minimum cabin -----------
    if spec.include_cash and "google_flights" in adapters:
        chosen.append("google_flights")
        cabins = sorted({spec.preferred_cabin, spec.min_cabin}, key=lambda c: -c.rank)
        for direction, days in directions:
            for dest, w in dests.items():
                if w < 0.9:
                    continue
                for org in origins[:1]:
                    o, d = (org, dest) if direction == "out" else (dest, org)
                    for i, day in enumerate(days):
                        for cab in cabins:
                            pqs.append(
                                PlannedQuery(
                                    Query("google_flights", "day", o, d, day, spec.passengers, cab),
                                    "cash",
                                    direction,
                                    priority=10
                                    + w
                                    - i * 1e-4
                                    + (0.01 if cab == spec.preferred_cabin else 0),
                                )
                            )
        notes.append(
            "cash fares (Google Flights) searched for primary destinations (weight >= 0.9)"
        )

    # ---- cache: fresh stored answers cost nothing -------------------------------
    if use_cache:
        fresh = fresh_index()
        for pq in pqs:
            ad = adapters[pq.query.adapter]
            ttl = ttl_for(pq.query, ad).total_seconds()
            ages = [fresh[k.key] for k in serving_queries(pq.query, ad) if k.key in fresh]
            pq.cached = bool(ages) and min(ages) <= ttl

    # ---- time budget -> per-adapter live capacity -------------------------------
    qps: dict[str, float] = {}
    acquire_s = spec.time_budget_minutes * 60 * ACQUIRE_SHARE
    cooling: dict[str, float] = {}
    for a in set(chosen):
        ad = adapters[a]
        lat = measured_latency_s(a, DEFAULT_LATENCY_S.get(a, 6.0))
        qps[a] = min(
            throughput_qps(DEFAULT_POLICIES.get(ad.site), lat), governed_qps(governor, ad.site)
        )
        cool = governor.cooldown_remaining(ad.site)
        if cool > 0:
            cooling[a] = cool
    pqs.sort(key=lambda p: -p.priority)
    used: dict[str, int] = {}
    for pq in pqs:
        if pq.cached:
            continue
        a = pq.query.adapter
        cap = max_queries_per_adapter
        if not deep:
            live_s = max(0.0, acquire_s - cooling.get(a, 0.0))
            budget_cap = int(qps[a] * live_s)
            cap = budget_cap if cap is None else min(cap, budget_cap)
        n = used.get(a, 0)
        if cap is not None and n >= cap:
            if a in cooling and cap == 0:
                pq.skip_reason = (
                    f"deferred: {adapters[a].site} is cooling down after a block "
                    f"(~{cooling[a] / 60:.0f} min left); the background crawler will fill it"
                )
            else:
                pq.skip_reason = (
                    f"deferred: beyond the {spec.time_budget_minutes:g}-min time budget "
                    f"(~{cap} live {a} queries fit); the background crawler will fill it "
                    "(or `jetbridge resume <search_id> --deep`)"
                )
        else:
            used[a] = n + 1
    est = max((n / qps[a] + cooling.get(a, 0.0) for a, n in used.items()), default=0.0)
    n_cached = sum(1 for p in pqs if p.cached)
    if n_cached:
        notes.append(
            f"{n_cached} of {len(pqs)} queries have a fresh stored answer and are served from "
            "the availability cache (no live request)"
        )
    for a, cool in sorted(cooling.items()):
        notes.append(f"{a}: cooling down after a block for ~{cool / 60:.0f} more min")
    if not deep:
        est += spec.time_budget_minutes * 60 * (1 - ACQUIRE_SHARE)  # refine + reserves
    deferred = sum(1 for p in pqs if p.skip_reason)
    if deferred:
        by_dest: dict[str, int] = {}
        for p in pqs:
            if p.skip_reason:
                end = p.query.destination if p.direction == "out" else p.query.origin
                by_dest[end] = by_dest.get(end, 0) + 1
        top = ", ".join(f"{k} ({v})" for k, v in sorted(by_dest.items(), key=lambda x: -x[1])[:8])
        notes.append(f"{deferred} lower-priority queries deferred to fit the time budget: {top}")
    if not spec.include_positioning:
        notes.append("positioning (separate-ticket) composition disabled by spec")
    return SearchPlan(pqs, origins, dests, sorted(set(chosen)), excluded, est, qps, notes)
