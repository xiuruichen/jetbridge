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
# Seats are often released close to departure, and just after a program's window opens, so more of
# the searching goes there. This is a rule of thumb about where searches are best spent, not a
# prediction of when any flight will open.
CLOSE_IN_DAYS = 14
FAR_OUT_DAYS = 60
MAX_CHECKS_PER_DAY = 24
MIN_FAR_OUT_CHECKS_PER_DAY = 2
# A slower date still waits for this share of its gap, so it stays roughly on its own pace.
DUE_SHARE = 0.9


def next_check_at(now: float, checks_per_day: float, rng: random.Random) -> float:
    """Always counted from now, so downtime yields one catch-up check and never a burst."""
    return now + DAY_S / checks_per_day * rng.uniform(*JITTER)


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


def checks_per_day_on(day: date, program: str, base: int, today: date) -> float:
    """How often to check this date today, given the watch's own setting."""
    days_out = (day - today).days
    just_opened = days_out >= get_site(program).booking_window_days - OPENS_EARLY_DAYS
    if days_out <= CLOSE_IN_DAYS or just_opened:
        return float(min(base * 2, MAX_CHECKS_PER_DAY))
    if days_out > FAR_OUT_DAYS:
        return max(base / 2, float(min(base, MIN_FAR_OUT_CHECKS_PER_DAY)))
    return float(base)


def fastest_checks_per_day(watch: Watch, today: date) -> float:
    """The pace a watch has to wake up at, so its busiest date stays on schedule."""
    rates = [
        checks_per_day_on(day, watch.program, watch.checks_per_day, today)
        for day in searchable_dates(watch, today)
    ]
    return max(rates, default=float(watch.checks_per_day))


def date_is_due(
    watch: Watch, day: date, last_clear_at: float | None, now: float, today: date
) -> bool:
    """Whether this date wants a search on this visit. Until it gets a clear answer, it does."""
    if last_clear_at is None:
        return True
    rate = checks_per_day_on(day, watch.program, watch.checks_per_day, today)
    if rate >= fastest_checks_per_day(watch, today):
        return True
    return now - last_clear_at >= DAY_S / rate * DUE_SHARE


def normal_gap_s(watch: Watch, day: date, today: date) -> float:
    """The longest this date can healthily go between answers."""
    rate = checks_per_day_on(day, watch.program, watch.checks_per_day, today)
    fastest = fastest_checks_per_day(watch, today)
    if rate >= fastest:
        return DAY_S / rate * JITTER[1]
    return DAY_S / rate * DUE_SHARE + DAY_S / fastest * JITTER[1]


def searches_per_day(
    days: tuple[date, ...], program: str, base: int, today: date
) -> tuple[int, int]:
    """About how many searches these dates use a day: today, and at their busiest."""
    upcoming = [day for day in days if day >= today]
    now = sum(
        checks_per_day_on(day, program, base, today)
        for day in upcoming
        if is_searchable(day, program, today)
    )
    return round(now), len(upcoming) * min(base * 2, MAX_CHECKS_PER_DAY)


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
