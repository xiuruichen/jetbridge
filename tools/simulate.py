"""Compare fixed and adaptive checking on made-up patterns of seats opening. Offline.

    uv run python tools/simulate.py

These patterns are invented, so the numbers show how the scheduling behaves under each assumption.
They don't say how often real seats will be caught. It uses the real scheduling code, including
its limits, and models one watch on one date.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, datetime

from jetbridge.models import Cabin, Flight, Watch
from jetbridge.scheduler import (
    Burst,
    DateView,
    burst_is_due,
    next_check_at,
    next_visit_at,
    spend_burst,
    start_burst,
    why_due,
)

TICK_S = 900.0
HOUR = 3600.0
DAY_S = 86400.0
DEPARTURE = date(2027, 6, 1)
START_DAYS_OUT = 120
RUNS = 300


@dataclass(frozen=True)
class Opening:
    start: float
    length: float


def _at(days_out: float) -> float:
    departure = datetime(DEPARTURE.year, DEPARTURE.month, DEPARTURE.day, 12).timestamp()
    return departure - days_out * DAY_S


def _no_pattern(rng: random.Random) -> list[Opening]:
    """Any day is as likely as any other, and it's over within a few hours."""
    return [
        Opening(_at(rng.uniform(1, START_DAYS_OUT)), rng.uniform(1, 8) * HOUR) for _ in range(4)
    ]


def _close_in(rng: random.Random) -> list[Opening]:
    """Mostly in the last two weeks."""
    return [Opening(_at(rng.uniform(1, 14)), rng.uniform(1, 8) * HOUR) for _ in range(4)]


def _flurries(rng: random.Random) -> list[Opening]:
    """Short openings that come in clusters, as when inventory is being moved around."""
    found = []
    for _ in range(2):
        start = _at(rng.uniform(5, START_DAYS_OUT))
        found.extend(
            Opening(start + rng.uniform(0, 8) * HOUR, rng.uniform(0.5, 2) * HOUR) for _ in range(3)
        )
    return found


def _rare_and_short(rng: random.Random) -> list[Opening]:
    """One or two brief openings at any time, where most schedules will miss them."""
    return [
        Opening(_at(rng.uniform(1, START_DAYS_OUT)), rng.uniform(0.5, 1.5) * HOUR)
        for _ in range(rng.randint(1, 2))
    ]


PATTERNS = {
    "no pattern at all": _no_pattern,
    "mostly in the last two weeks": _close_in,
    "short flurries": _flurries,
    "rare and short": _rare_and_short,
}


def _is_open(openings: list[Opening], at: float) -> bool:
    return any(o.start <= at < o.start + o.length for o in openings)


def _caught(openings: list[Opening], checks: list[float]) -> int:
    return sum(any(o.start <= c < o.start + o.length for c in checks) for o in openings)


def _fixed(openings: list[Opening], rng: random.Random, per_day: float) -> list[float]:
    now = _at(START_DAYS_OUT)
    end = _at(0)
    checks = []
    while now < end:
        checks.append(now)
        now = next_check_at(now, per_day, rng)
    return checks


def _adaptive(openings: list[Opening], rng: random.Random, base: int) -> list[float]:
    watch = Watch(
        1, Flight("CX", "805"), "JFK", "HKG", (DEPARTURE,), Cabin.BUSINESS, checks_per_day=base
    )
    now = _at(START_DAYS_OUT)
    end = _at(0)
    checks: list[float] = []
    last: float | None = None
    burst: Burst | None = None
    was_open = False
    visit = now
    while now < end:
        if now >= visit:
            today = datetime.fromtimestamp(now).date()
            reason = why_due(watch, DEPARTURE, DateView(last, last, burst), now, today)
            if reason is not None:
                checks.append(now)
                last = now
                if burst_is_due(burst, now):
                    burst = spend_burst(burst, now, rng)
                is_open = _is_open(openings, now)
                if is_open != was_open:
                    burst = start_burst(burst, "moved", now, rng)
                was_open = is_open
            visit = next_visit_at(watch, now, today, [burst] if burst else [], rng)
        now += TICK_S
    return checks


def compare() -> dict[str, dict[str, float]]:
    """For each pattern, the share of openings caught and the searches used by each schedule. The
    fixed schedule is given the same number of searches the adaptive one used, so it's fair."""
    results = {}
    for name, pattern in PATTERNS.items():
        rng = random.Random(7)
        totals = {"openings": 0, "adaptive": 0, "fixed": 0, "adaptive_checks": 0, "fixed_checks": 0}
        for _ in range(RUNS):
            openings = pattern(rng)
            adaptive = _adaptive(openings, rng, 6)
            per_day = len(adaptive) / START_DAYS_OUT
            fixed = _fixed(openings, rng, per_day)
            totals["openings"] += len(openings)
            totals["adaptive"] += _caught(openings, adaptive)
            totals["fixed"] += _caught(openings, fixed)
            totals["adaptive_checks"] += len(adaptive)
            totals["fixed_checks"] += len(fixed)
        results[name] = {
            "adaptive_caught": totals["adaptive"] / totals["openings"],
            "fixed_caught": totals["fixed"] / totals["openings"],
            "adaptive_checks": totals["adaptive_checks"] / RUNS,
            "fixed_checks": totals["fixed_checks"] / RUNS,
        }
    return results


def main() -> None:
    print("SIMULATED, using made-up patterns. This doesn't show real-world catch rates.\n")
    for name, row in compare().items():
        print(name)
        print(
            f"  adaptive: caught {row['adaptive_caught']:.0%} "
            f"with {row['adaptive_checks']:.0f} searches"
        )
        print(
            f"  fixed:    caught {row['fixed_caught']:.0%} with {row['fixed_checks']:.0f} searches"
        )


if __name__ == "__main__":
    main()
