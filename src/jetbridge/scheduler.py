"""What is due, when it next runs, and how checks share requests. Pure functions."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from datetime import date, timedelta

from jetbridge.acquire.sites import get_site
from jetbridge.models import Query, Watch

DAY_S = 86400.0
JITTER = (0.7, 1.3)
# Published booking windows can be a day or so out, and starting late could miss seats.
OPENS_EARLY_DAYS = 3


def next_check_at(now: float, checks_per_day: int, rng: random.Random) -> float:
    """Always counted from now, so downtime yields one catch-up check and never a burst."""
    return now + DAY_S / checks_per_day * rng.uniform(*JITTER)


def longest_gap_s(checks_per_day: int) -> float:
    """The longest a healthy watch can go between checks."""
    return DAY_S / checks_per_day * JITTER[1]


def is_due(watch: Watch, now: float) -> bool:
    return watch.status == "active" and watch.next_check_at <= now


def upcoming_dates(watch: Watch, today: date) -> tuple[date, ...]:
    return tuple(d for d in watch.dates if d >= today)


def search_starts(day: date, program: str) -> date:
    """The first day it's worth searching for this date on this program."""
    return day - timedelta(days=get_site(program).booking_window_days + OPENS_EARLY_DAYS)


def is_searchable(day: date, program: str, today: date) -> bool:
    return search_starts(day, program) <= today <= day


def searchable_dates(watch: Watch, today: date) -> tuple[date, ...]:
    """Upcoming dates the program can already show. Anything further out would be wasted."""
    return tuple(d for d in watch.dates if is_searchable(d, watch.program, today))


def is_expired(watch: Watch, today: date) -> bool:
    return not upcoming_dates(watch, today)


def group_queries(
    watches: list[Watch], today: date, wanted: Callable[[Watch, date], bool] | None = None
) -> dict[Query, list[Watch]]:
    """One request per route, date and real party size, however many watches share it."""
    groups: dict[Query, list[Watch]] = defaultdict(list)
    for watch in watches:
        for day in searchable_dates(watch, today):
            if wanted is not None and not wanted(watch, day):
                continue
            groups[Query(watch.origin, watch.destination, day, watch.pax, watch.program)].append(
                watch
            )
    return dict(
        sorted(groups.items(), key=lambda kv: (kv[0].program, kv[0].date, kv[0].origin, kv[0].pax))
    )
