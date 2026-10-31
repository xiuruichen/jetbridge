"""Multi-passenger allocation with CP-SAT.

Decision: how many passengers ride on each candidate offer, and how each
program's miles are funded (own balance vs. transfers from card currencies).

Why a real solver: card currencies (e.g. Bilt) fund several programs at once,
so "cheapest per passenger" choices interact through shared balances; and the
"travel together" policy couples passengers to the same flights/day.

Soundness of passenger counts per offer (``max_pax_for``):
* An offer observed with a search for r passengers is valid for exactly r
  passengers at that price (aa.com prices change with party size).
* If the adapter is verified ``price_independent_of_pax``, it may take any
  k <= known seats.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from ortools.sat.python import cp_model

from jetbridge.kb import KB
from jetbridge.models.core import AwardOffer, Cabin
from jetbridge.models.spec import Profile, SearchSpec
from jetbridge.solve.eligibility import Candidate


@dataclass
class Assignment:
    candidate: Candidate
    pax: int


@dataclass
class Transfer:
    source: str
    program: str
    points: int  # card points sent
    miles: int  # program miles received


@dataclass
class Solution:
    assignments: list[Assignment]
    transfers: list[Transfer]
    own_used: dict[str, int]
    objective_cents: int
    value_cents: int  # points valued at cpp + cash (no preference penalties)
    label: str = ""
    warnings: list[str] = field(default_factory=list)

    def key(self) -> tuple:
        return tuple(sorted((a.candidate.id, a.pax) for a in self.assignments))

    @property
    def miles_by_program(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for a in self.assignments:
            if isinstance(a.candidate.offer, AwardOffer):
                out[a.candidate.offer.program] += a.candidate.offer.miles * a.pax
        return dict(out)

    @property
    def cash_usd(self) -> Decimal:
        return sum((a.candidate.cash_per_pax * a.pax for a in self.assignments), Decimal(0))


def max_pax_for(c: Candidate, n: int, independent: dict[str, bool]) -> tuple[int, int]:
    """(min, max) passengers assignable to this candidate when used."""
    o = c.offer
    r = o.provenance.request_pax
    seats = o.seats.count if o.seats else r
    if isinstance(o, AwardOffer) and independent.get(o.provenance.adapter, False):
        lo, hi = 1, max(1, min(n, max(seats, r)))
    else:
        lo = hi = min(r, n)
    # Cash positioning legs searched for R travellers: using them for fewer is
    # allowed (the per-pax price for R is a conservative upper bound).
    for x in c.positioning:
        hi = min(hi, x.provenance.request_pax)
    if lo > hi:
        return 0, 0
    return lo, hi


def _penalty(c: Candidate, spec: SearchSpec, w: object = None) -> int:
    """Per-pax disutility in cents (quality + destination + structural risk)."""
    return int(round(c.penalty_usd * 100))


def _cash_cents_c(c: Candidate) -> int:
    return int(c.cash_per_pax * 100)


class Allocator:
    def __init__(
        self,
        spec: SearchSpec,
        profile: Profile,
        kb: KB,
        independent: dict[str, bool],
        weights: object = None,
    ):
        self.spec, self.profile, self.kb = spec, profile, kb
        self.independent = independent
        self.w = weights
        self.constrain_balances = bool(profile.balances)

    def cpp(self, currency: str) -> Decimal:
        return self.kb.cpp_for(currency, self.profile.cpp_overrides)

    def solve(
        self,
        cands: list[Candidate],
        *,
        k: int = 12,
        time_limit_s: float = 20.0,
        restrict: dict | None = None,
    ) -> list[Solution]:
        """Up to ``k`` diverse solutions in objective order (no-good cuts)."""
        n = self.spec.passengers
        directions = ["out"] + (["in"] if self.spec.inbound else [])
        if restrict:
            cands = [c for c in cands if _matches(c, restrict)]
        if not all(any(c.direction == d for c in cands) for d in directions):
            return []

        cands = [c for c in cands if max_pax_for(c, n, self.independent)[1] > 0]
        ids = [c.id for c in cands]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate candidate ids: would alias solver variables")
        m = cp_model.CpModel()
        x: dict[str, cp_model.IntVar] = {}
        use: dict[str, cp_model.IntVar] = {}
        bounds = {}
        for c in cands:
            lo, hi = max_pax_for(c, n, self.independent)
            bounds[c.id] = (lo, hi)
            x[c.id] = m.new_int_var(0, hi, f"x_{c.id}")
            use[c.id] = m.new_bool_var(f"u_{c.id}")
            m.add(x[c.id] >= max(lo, 1)).only_enforce_if(use[c.id])
            m.add(x[c.id] == 0).only_enforce_if(use[c.id].Not())

        groups: dict[tuple[str, str], cp_model.IntVar] = {}
        for d in directions:
            dc = [c for c in cands if c.direction == d]
            m.add(sum(x[c.id] for c in dc) == n)
            if self.spec.together != "any":
                keyf = (
                    (lambda c: c.group)
                    if self.spec.together == "same_flights"
                    else (lambda c: c.offer.itinerary.departs_at.date().isoformat())
                )
                gmap: dict[str, list[Candidate]] = defaultdict(list)
                for c in dc:
                    gmap[keyf(c)].append(c)
                ys = []
                for g, members in gmap.items():
                    y = m.new_bool_var(f"g_{d}_{g}")
                    groups[(d, g)] = y
                    for c in members:
                        m.add_implication(use[c.id], y)
                    ys.append(y)
                m.add_exactly_one(ys)

        # ---- funding ---------------------------------------------------------
        programs = sorted({c.offer.program for c in cands if isinstance(c.offer, AwardOffer)})
        own: dict[str, cp_model.IntVar] = {}
        tvar: dict[tuple[str, str], cp_model.IntVar] = {}
        cost_terms = []
        BIG = 10_000_000
        for p in programs:
            need = sum(
                c.offer.miles * x[c.id]  # type: ignore[union-attr]
                for c in cands
                if isinstance(c.offer, AwardOffer) and c.offer.program == p
            )
            bal = self.profile.balances.get(p, 0) if self.constrain_balances else BIG
            own[p] = m.new_int_var(0, bal, f"own_{p}")
            incoming = []
            for t in self.kb.funding_sources(p)[1:]:
                cbal = self.profile.balances.get(t.source, 0) if self.constrain_balances else 0
                if cbal <= 0:
                    continue
                # transfers in 1,000-point blocks (card side)
                blocks = m.new_int_var(0, cbal // 1000, f"t_{t.source}_{p}")
                tvar[(t.source, p)] = blocks
                incoming.append(int(1000 * t.ratio) * blocks)
                cost_terms.append(_cpp_int(self.cpp(t.source)) * 1000 * blocks)
            m.add(own[p] + sum(incoming) >= need)
            cost_terms.append(_cpp_int(self.cpp(self.kb.programs[p].currency)) * own[p])
        for src in {s for s, _ in tvar}:
            m.add(
                sum(1000 * tvar[(s, p)] for (s, p) in tvar if s == src)
                <= self.profile.balances[src]
            )

        # ---- objective (cents * 100 to keep cpp precision) -------------------
        for c in cands:
            per = (_cash_cents_c(c) + _penalty(c, self.spec)) * 100
            cost_terms.append(per * x[c.id])
        m.minimize(sum(cost_terms))

        sols: list[Solution] = []
        for _ in range(k):
            solver = cp_model.CpSolver()
            solver.parameters.max_time_in_seconds = time_limit_s
            solver.parameters.num_workers = 8
            st = solver.solve(m)
            if st not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                break
            chosen = [c for c in cands if solver.value(x[c.id]) > 0]
            sol = Solution(
                assignments=[Assignment(c, solver.value(x[c.id])) for c in chosen],
                transfers=[
                    Transfer(
                        s,
                        p,
                        1000 * solver.value(v),
                        int(1000 * _ratio(self.kb, s, p)) * solver.value(v),
                    )
                    for (s, p), v in tvar.items()
                    if solver.value(v) > 0
                ],
                own_used={p: solver.value(v) for p, v in own.items() if solver.value(v) > 0},
                objective_cents=int(solver.objective_value // 100),
                value_cents=0,
            )
            sol.own_used = _tight_own(sol)
            sol.value_cents = self._value(sol)
            if st == cp_model.FEASIBLE:
                sol.warnings.append(
                    "solver hit time limit; solution feasible but not proven optimal"
                )
            sols.append(sol)
            # no-good cut on the set of used candidates
            m.add(sum(use[c.id] for c in chosen) <= len(chosen) - 1)
        return sols

    def _value(self, sol: Solution) -> int:
        cents = Decimal(0)
        for p, used in sol.own_used.items():
            cents += Decimal(used) * self.cpp(self.kb.programs[p].currency)
        for t in sol.transfers:
            cents += Decimal(t.points) * self.cpp(t.source)
        cents += sol.cash_usd * 100
        return int(cents)


def _tight_own(sol: Solution) -> dict[str, int]:
    """Own-balance usage = need - transferred (solver may over-cover)."""
    need = sol.miles_by_program
    incoming: dict[str, int] = defaultdict(int)
    for t in sol.transfers:
        incoming[t.program] += t.miles
    return {p: max(0, n - incoming[p]) for p, n in need.items() if n - incoming[p] > 0}


def _ratio(kb: KB, src: str, prog: str) -> float:
    return next(t.ratio for t in kb.transfers if t.source == src and t.program == prog)


def _cpp_int(cpp: Decimal) -> int:
    # objective is in cents*100; a point worth `cpp` cents costs cpp*100
    return int(cpp * 100)


def _matches(c: Candidate, r: dict) -> bool:
    o = c.offer
    if "cabin" in r and o.itinerary.main_cabin != r["cabin"]:
        return False
    if "program" in r:
        if not isinstance(o, AwardOffer) or o.program != r["program"]:
            return False
    if "award_only" in r and not isinstance(o, AwardOffer):
        return False
    if "composed" in r and c.composed != r["composed"]:
        return False
    return True


__all__ = ["Allocator", "Assignment", "Cabin", "Solution", "Transfer", "max_pax_for"]
