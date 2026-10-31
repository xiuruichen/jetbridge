"""SearchSpec -> SearchPlan.

Pricing rule learned in live recon (2026-10-31): aa.com dynamic award prices
change with party size (352,500 for 1 pax vs 384,000/pax for 3 on the same
flights), and a 1-pax search can show seats that do not exist for 3. So:

* **Primary stage** searches with the *real* passenger count. Its prices are
  authoritative for the whole party.
* **Split stage** (only when passengers > 1) searches pax=1 on preferred
  destinations to find space that only works as a split booking (e.g. 2 seats
  on one flight + 1 on another). The solver only assigns such an offer as many
  passengers as is *sound* for its adapter (see ``Adapter.price_independent_of_pax``).

Nothing is pruned heuristically. When a per-adapter query budget is exceeded,
the overflow is written to the ledger as ``skipped`` with the reason, so the
report shows exactly what was not searched.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.engine import DEFAULT_POLICIES
from jetbridge.acquire.types import Query
from jetbridge.kb import KB
from jetbridge.models.spec import Profile, SearchSpec

EST_LATENCY_S = {
    "united": 4.0,
    "american": 9.0,
    "delta": 5.0,
    "alaska": 5.0,
    "google_flights": 12.0,
}


@dataclass
class PlannedQuery:
    query: Query
    stage: str  # primary|split
    direction: str  # out|in
    priority: float
    skip_reason: str | None = None

    def to_dict(self) -> dict:
        return {
            "query": self.query.to_dict(),
            "stage": self.stage,
            "direction": self.direction,
            "priority": self.priority,
            "skip_reason": self.skip_reason,
        }


@dataclass
class SearchPlan:
    queries: list[PlannedQuery]
    origins: list[str]
    destinations: dict[str, float]  # airport -> preference weight
    adapters: list[str]
    excluded_programs: dict[str, str]  # program -> reason it is not searched
    est_seconds: float
    notes: list[str] = field(default_factory=list)

    @property
    def runnable(self) -> list[PlannedQuery]:
        return [q for q in self.queries if q.skip_reason is None]

    def summary(self) -> dict:
        by_adapter: dict[str, int] = {}
        for pq in self.runnable:
            by_adapter[pq.query.adapter] = by_adapter.get(pq.query.adapter, 0) + 1
        return {
            "origins": self.origins,
            "destinations": self.destinations,
            "adapters": self.adapters,
            "excluded_programs": self.excluded_programs,
            "queries_total": len(self.queries),
            "queries_runnable": len(self.runnable),
            "queries_skipped": len(self.queries) - len(self.runnable),
            "by_adapter": by_adapter,
            "estimated_minutes": round(self.est_seconds / 60, 1),
            "notes": self.notes,
        }

    def to_dict(self) -> dict:
        return {"summary": self.summary(), "queries": [q.to_dict() for q in self.queries]}


def _reachable(program: str, profile: Profile, kb: KB) -> tuple[bool, str]:
    if not profile.balances:
        return True, "no balances in profile: all programs searched"
    for src in kb.funding_sources(program):
        if profile.balances.get(src.source, 0) > 0:
            return True, f"funded by {src.source}"
    return False, "no balance in this program or any card currency that transfers to it"


def build_plan(
    spec: SearchSpec,
    profile: Profile,
    kb: KB,
    adapters: dict[str, Adapter],
    *,
    max_queries_per_adapter: int = 700,
    split_stage: bool = True,
) -> SearchPlan:
    notes: list[str] = []
    excluded: dict[str, str] = {}

    # ---- which adapters ---------------------------------------------------
    wanted_programs = spec.programs or list(kb.programs)
    chosen: list[str] = []
    for pid in wanted_programs:
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

    # ---- geography ----------------------------------------------------------
    origins: list[str] = []
    for o in [*spec.origins, *spec.nearby_origins]:
        for ap in kb.expand(o):
            if ap not in origins:
                origins.append(ap)
    dests: dict[str, float] = {}
    for d in spec.destinations:
        for ap in kb.expand(d.code):
            dests[ap] = max(dests.get(ap, 0.0), d.weight)
    for ap in list(dests):
        if ap in origins:
            del dests[ap]

    # ---- queries ------------------------------------------------------------
    pqs: list[PlannedQuery] = []
    directions = [("out", spec.outbound.days())]
    if spec.inbound:
        directions.append(("in", spec.inbound.days()))
    for direction, days in directions:
        for a in chosen:
            for dest, w in dests.items():
                for org in origins:
                    o, d = (org, dest) if direction == "out" else (dest, org)
                    home = 1.0 if org == spec.origins[0] else 0.8
                    for i, day in enumerate(days):
                        base = w * home - i * 1e-4
                        pqs.append(
                            PlannedQuery(
                                Query(a, "day", o, d, day, spec.passengers),
                                "primary",
                                direction,
                                priority=2.0 + base,
                            )
                        )
                        if split_stage and spec.passengers > 1 and w >= 0.9 and home == 1.0:
                            pqs.append(
                                PlannedQuery(
                                    Query(a, "day", o, d, day, 1),
                                    "split",
                                    direction,
                                    priority=1.0 + base,
                                )
                            )

    # ---- cash fares (real party size; preferred + minimum cabin) -------------
    if spec.include_cash and "google_flights" in adapters:
        chosen.append("google_flights")
        cabins = sorted({spec.preferred_cabin, spec.min_cabin}, key=lambda c: -c.rank)
        for direction, days in directions:
            for dest, w in dests.items():
                if w < 0.9:
                    continue  # cash baseline: primary destinations only (stated in notes)
                for org in origins:
                    o, d = (org, dest) if direction == "out" else (dest, org)
                    home = 1.0 if org == spec.origins[0] else 0.8
                    for i, day in enumerate(days):
                        for cab in cabins:
                            pqs.append(
                                PlannedQuery(
                                    Query("google_flights", "day", o, d, day, spec.passengers, cab),
                                    "cash",
                                    direction,
                                    priority=1.5
                                    + w * home
                                    - i * 1e-4
                                    + (0.01 if cab == spec.preferred_cabin else 0),
                                )
                            )

    # ---- budget (explicit, recorded as skipped) ----------------------------
    pqs.sort(key=lambda p: -p.priority)
    used: dict[str, int] = {}
    for pq in pqs:
        n = used.get(pq.query.adapter, 0)
        if n >= max_queries_per_adapter:
            pq.skip_reason = f"query budget ({max_queries_per_adapter}/adapter) exhausted"
        else:
            used[pq.query.adapter] = n + 1

    est = 0.0
    for a, n in used.items():
        pol = DEFAULT_POLICIES.get(adapters[a].site)
        gap = (pol.min_interval_s + pol.max_interval_s) / 2 if pol else 5.5
        est = max(est, n * (gap + EST_LATENCY_S.get(a, 6.0)))
    if spec.include_cash and "google_flights" in adapters:
        notes.append(
            "cash fares (Google Flights) searched for primary destinations (weight >= 0.9) only"
        )
    skipped = sum(1 for p in pqs if p.skip_reason)
    if skipped:
        notes.append(f"{skipped} lower-priority queries exceed the budget and will be skipped")
    if not spec.include_positioning:
        notes.append("positioning (separate-ticket) composition disabled by spec")
    else:
        notes.append(
            "through-itineraries from each airline's own engine are searched; "
            "separate-ticket gateway composition is not yet implemented"
        )
    return SearchPlan(pqs, origins, dests, chosen, excluded, est, notes)
