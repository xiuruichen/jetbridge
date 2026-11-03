"""What is due, when it next runs, and how checks share requests. Pure functions."""

from __future__ import annotations

import random
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from jetbridge.acquire.sites import get_site
from jetbridge.models import Query, Watch

DAY_S = 86400.0
JITTER = (0.7, 1.3)
# Published booking windows can be a day or so out, and starting late could miss seats.
OPENS_EARLY_DAYS = 3
# Seats are often released close to departure, and just after a program's window opens, so more of
# the searching goes there. This is a rule of thumb about where searches are best spent, not a
# prediction of when any flight will open. The sizes come from tools/simulate.py: a stronger tilt
# catches more when seats do open late, but misses more when there's no pattern at all.
CLOSE_IN_DAYS = 14
FAR_OUT_DAYS = 60
BUSY_PACE = 1.5
FAR_OUT_PACE = 5 / 6
MAX_CHECKS_PER_DAY = 24
MIN_FAR_OUT_CHECKS_PER_DAY = 2
# A slower date still waits for this share of its gap, so it stays roughly on its own pace.
DUE_SHARE = 0.9
HIGH_PRIORITY_PACE = 1.5
# Even with a site's budget spent, no date goes longer than this between answers.
MIN_CHECKS_PER_DAY = 2
# Movement on a date brings its next checks forward, this far apart, and no more often than this.
BURST_GAPS_S = (3600.0, 7200.0, 14400.0)
BURST_PERIOD_S = DAY_S
# A date first bookable tonight gets one check just after midnight at the airline's home, if this
# computer is awake then. One old report and one vendor mention midnight, so it's cheap to try.
FIRST_NIGHT_DELAY_S = 300.0
FIRST_NIGHT_WINDOW_S = 6 * 3600.0
NEIGHBOUR_DAYS = 3

FIRST_LOOK = "first_look"
FIRST_NIGHT = "first_night"
MOVED = "moved"
CLOSE_IN = "close_in"
JUST_OPENED = "just_opened"
SCHEDULE = "schedule"
MANUAL = "manual"

# Said to people, so these describe what happened and never what's about to happen.
WHY = {
    FIRST_LOOK: "this date hadn't had a clear answer yet",
    FIRST_NIGHT: "this date just became bookable",
    MOVED: "seats or prices moved recently",
    CLOSE_IN: "this date is within two weeks",
    JUST_OPENED: "this date only recently became bookable",
    SCHEDULE: "its regular schedule",
    MANUAL: "someone asked for a check",
}
_SCORES = {FIRST_LOOK: 50, FIRST_NIGHT: 60, MOVED: 40, CLOSE_IN: 30, JUST_OPENED: 20, SCHEDULE: 10}


@dataclass(frozen=True)
class Burst:
    """A few extra checks after movement on a date. Spends from the budget, and never adds to it."""

    reason: str
    started_at: float
    used: int = 0
    next_at: float | None = None


def start_burst(current: Burst | None, reason: str, now: float, rng: random.Random) -> Burst | None:
    """Bring the next check forward, unless that's already planned or this date has had its share
    in the last day. More movement doesn't add more checks."""
    if current is not None and now - current.started_at < BURST_PERIOD_S:
        if current.next_at is not None or current.used >= len(BURST_GAPS_S):
            return current
        return replace(current, reason=reason, next_at=_after(current.used, now, rng))
    return Burst(reason, now, 0, _after(0, now, rng))


def spend_burst(current: Burst, now: float, rng: random.Random) -> Burst:
    used = current.used + 1
    following = _after(used, now, rng) if used < len(BURST_GAPS_S) else None
    return replace(current, used=used, next_at=following)


def _after(used: int, now: float, rng: random.Random) -> float:
    return now + BURST_GAPS_S[used] * rng.uniform(*JITTER)


def burst_is_due(current: Burst | None, now: float) -> bool:
    return current is not None and current.next_at is not None and current.next_at <= now


def first_night_at(day: date, program: str) -> float:
    """Just after midnight at the airline's home, on the day this date should become bookable."""
    site = get_site(program)
    opens = day - timedelta(days=site.booking_window_days)
    midnight = datetime.combine(opens, time.min, ZoneInfo(site.home_timezone))
    return midnight.timestamp() + FIRST_NIGHT_DELAY_S


def first_night_is_due(day: date, program: str, last_check_at: float | None, now: float) -> bool:
    at = first_night_at(day, program)
    return at <= now < at + FIRST_NIGHT_WINDOW_S and (last_check_at is None or last_check_at < at)


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


def opens_on(day: date, program: str) -> date:
    """The day this date should become bookable, going by the program's published window."""
    return day - timedelta(days=get_site(program).booking_window_days)


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
        return min(base * BUSY_PACE, float(MAX_CHECKS_PER_DAY))
    if days_out > FAR_OUT_DAYS:
        return max(base * FAR_OUT_PACE, float(min(base, MIN_FAR_OUT_CHECKS_PER_DAY)))
    return float(base)


def watch_checks_per_day(watch: Watch, day: date, today: date) -> float:
    """This date's pace on this watch, including a watch someone said matters most."""
    rate = checks_per_day_on(day, watch.program, watch.checks_per_day, today)
    return min(rate * HIGH_PRIORITY_PACE, MAX_CHECKS_PER_DAY) if watch.high_priority else rate


def fastest_checks_per_day(watch: Watch, today: date) -> float:
    """The pace a watch has to wake up at, so its busiest date stays on schedule."""
    rates = [watch_checks_per_day(watch, day, today) for day in searchable_dates(watch, today)]
    return max(rates, default=float(watch.checks_per_day))


def date_is_due(
    watch: Watch, day: date, last_clear_at: float | None, now: float, today: date
) -> bool:
    """Whether this date wants a search on this visit. Until it gets a clear answer, it does."""
    if last_clear_at is None:
        return True
    rate = watch_checks_per_day(watch, day, today)
    if rate >= fastest_checks_per_day(watch, today):
        return True
    return now - last_clear_at >= DAY_S / rate * DUE_SHARE


def normal_gap_s(watch: Watch, day: date, today: date) -> float:
    """The longest this date can healthily go between answers."""
    rate = watch_checks_per_day(watch, day, today)
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
    return round(now), round(len(upcoming) * min(base * BUSY_PACE, MAX_CHECKS_PER_DAY))


@dataclass(frozen=True)
class DateView:
    """What's recorded about one date, as far as deciding its next search goes."""

    last_clear_at: float | None = None
    last_check_at: float | None = None
    burst: Burst | None = None


def why_due(watch: Watch, day: date, view: DateView, now: float, today: date) -> str | None:
    """Why this date wants a search right now, or None if it doesn't."""
    if view.last_clear_at is None:
        return FIRST_LOOK
    if first_night_is_due(day, watch.program, view.last_check_at, now):
        return FIRST_NIGHT
    if burst_is_due(view.burst, now):
        return MOVED
    if not date_is_due(watch, day, view.last_clear_at, now, today):
        return None
    days_out = (day - today).days
    if days_out <= CLOSE_IN_DAYS:
        return CLOSE_IN
    window = get_site(watch.program).booking_window_days
    return JUST_OPENED if days_out >= window - OPENS_EARLY_DAYS else SCHEDULE


def _must_search(watch: Watch, view: DateView, now: float) -> bool:
    """Past its minimum pace, so this date goes ahead even with the day's budget spent."""
    if view.last_clear_at is None:
        return True
    minimum = min(watch.checks_per_day, MIN_CHECKS_PER_DAY)
    return now - view.last_clear_at >= DAY_S / minimum


@dataclass(frozen=True)
class PlannedSearch:
    watches: tuple[Watch, ...]
    reason: str


def plan_searches(
    watches: list[Watch],
    today: date,
    now: float,
    view_of: Callable[[Watch, date], DateView],
    room_of: Callable[[str], int],
) -> dict[Query, PlannedSearch]:
    """Which searches to run this time, most deserving first. One search per route, date and party
    size, however many watches share it. Once a site's budget for the day is spent, only dates past
    their minimum pace still go ahead, and the governor's hard limits apply to those as always."""
    wanted: dict[Query, list[tuple[Watch, str, bool]]] = defaultdict(list)
    for watch in watches:
        for day in searchable_dates(watch, today):
            view = view_of(watch, day)
            reason = why_due(watch, day, view, now, today)
            if reason is not None:
                query = Query(watch.origin, watch.destination, day, watch.pax, watch.program)
                wanted[query].append((watch, reason, _must_search(watch, view, now)))

    def score(query: Query) -> int:
        return max(
            _SCORES[reason] + (5 if watch.high_priority else 0)
            for watch, reason, _ in wanted[query]
        )

    ordered = sorted(
        wanted,
        key=lambda q: (q.program, -score(q), q.date, q.origin, q.destination, q.pax),
    )
    room: dict[str, int] = {}
    planned: dict[Query, PlannedSearch] = {}
    for query in ordered:
        site = query.program
        room.setdefault(site, room_of(site))
        cost = get_site(site).requests_per_search
        entries = wanted[query]
        if room[site] < cost and not any(must for _, _, must in entries):
            continue
        room[site] -= cost
        best = max(entries, key=lambda entry: _SCORES[entry[1]])
        planned[query] = PlannedSearch(tuple(entry[0] for entry in entries), best[1])
    return planned


def next_visit_at(
    watch: Watch, now: float, today: date, bursts: list[Burst], rng: random.Random
) -> float:
    """When to look at this watch again: its pace, or sooner for a burst or a date's first night."""
    at = next_check_at(now, fastest_checks_per_day(watch, today), rng)
    soonest = [burst.next_at for burst in bursts if burst.next_at is not None]
    for day in searchable_dates(watch, today):
        first_night = first_night_at(day, watch.program)
        if first_night > now:
            soonest.append(first_night)
    return min([at, *soonest])


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
