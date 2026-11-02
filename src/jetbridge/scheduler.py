"""What is due, when it next runs, and how checks share requests. Pure functions."""

from __future__ import annotations

import random
from collections import defaultdict
from datetime import date

from jetbridge.models import Query, Watch

DAY_S = 86400.0
JITTER = (0.7, 1.3)


def next_check_at(now: float, checks_per_day: int, rng: random.Random) -> float:
    """Always counted from now, so downtime yields one catch-up check and never a burst."""
    return now + DAY_S / checks_per_day * rng.uniform(*JITTER)


def is_due(watch: Watch, now: float) -> bool:
    return watch.status == "active" and watch.next_check_at <= now


def upcoming_dates(watch: Watch, today: date) -> tuple[date, ...]:
    return tuple(d for d in watch.dates if d >= today)


def is_expired(watch: Watch, today: date) -> bool:
    return not upcoming_dates(watch, today)


def group_queries(watches: list[Watch], today: date) -> dict[Query, list[Watch]]:
    """One request per route, date and real party size, however many watches share it."""
    groups: dict[Query, list[Watch]] = defaultdict(list)
    for watch in watches:
        for day in upcoming_dates(watch, today):
            groups[Query(watch.origin, watch.destination, day, watch.pax, watch.program)].append(
                watch
            )
    return dict(
        sorted(groups.items(), key=lambda kv: (kv[0].program, kv[0].date, kv[0].origin, kv[0].pax))
    )
