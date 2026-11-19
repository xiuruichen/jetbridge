"""A watch's flexible scope: nearby dates and other airports that would do just as well.

Pure functions. The scouting itself (the searches) is planned in ``scheduler`` and run in
``runner``.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import date, timedelta

from jetbridge.models import Query, Watch

MAX_FLEX_DAYS = 3
MAX_SCOUT_QUERIES = 30  # per watch, so a wide scope can't swallow the day's searches

# Airports with real long-haul service, so "anywhere in North America" doesn't mean every airfield.
REGIONS: dict[str, tuple[str, ...]] = {
    "north-america": tuple(
        [
            "YYZ",
            "YVR",
            "YUL",
            "JFK",
            "EWR",
            "BOS",
            "IAD",
            "ORD",
            "ATL",
            "MIA",
            "DFW",
            "IAH",
            "DEN",
            "SEA",
            "SFO",
            "LAX",
        ]
    ),
    "us-east-coast": ("JFK", "EWR", "BOS", "IAD", "ATL", "MIA"),
    "us-west-coast": ("SEA", "SFO", "LAX"),
    "canada": ("YYZ", "YVR", "YUL"),
    "new-york-area": ("JFK", "EWR", "LGA"),
    "london-area": ("LHR", "LGW"),
    "tokyo-area": ("HND", "NRT"),
    "paris-area": ("CDG", "ORY"),
    "bay-area": ("SFO", "SJC", "OAK"),
    "los-angeles-area": ("LAX", "BUR", "SNA", "ONT"),
}


def expand(text: str) -> tuple[str, ...]:
    """Turn 'north-america', 'JFK, EWR' or 'new york area' into airport codes, without repeats.

    Raises ValueError for a name it doesn't know, so nothing is saved on a guess."""
    from jetbridge.draft import resolve_airport  # draft imports this module too

    found: dict[str, None] = {}
    for part in text.split(","):
        name = "-".join(part.strip().lower().split())
        if not name:
            continue
        codes = REGIONS.get(name) or resolve_airport(part)
        if not codes:
            known = ", ".join(sorted(REGIONS))
            raise ValueError(
                f"I don't know {part.strip()!r}. Use airport codes, or one of: {known}"
            )
        found.update(dict.fromkeys(codes))
    return tuple(found)


def scout_queries(watch: Watch, today: date, searchable: Callable[[date], bool]) -> list[Query]:
    """Every search a watch's flexible scope adds, nearest to what was asked for first.

    The watch's own route on its own dates is left out, because the watch already covers those."""
    origins = (watch.origin, *(a for a in watch.flex_origins if a != watch.origin))
    destinations = (
        watch.destination,
        *(a for a in watch.flex_destinations if a != watch.destination),
    )
    seen: dict[Query, int] = {}
    for base in watch.dates:
        if base < today:
            continue
        for shift in range(-watch.flex_days, watch.flex_days + 1):
            day = base + timedelta(days=shift)
            if day < today or not searchable(day):
                continue
            for o_rank, origin in enumerate(origins):
                for d_rank, destination in enumerate(destinations):
                    if (origin, destination) == (watch.origin, watch.destination) and (
                        day in watch.dates
                    ):
                        continue
                    query = Query(origin, destination, day, watch.pax, watch.program)
                    rank = abs(shift) * 100 + o_rank + d_rank
                    seen[query] = min(rank, seen.get(query, rank))
    ordered = sorted(seen, key=lambda q: (seen[q], q.date, q.origin, q.destination))
    return ordered[:MAX_SCOUT_QUERIES]


def check(days: int, origins: str | None, destinations: str | None) -> None:
    """Raise ValueError if a scope would be refused, so a review never promises more than saves."""
    if not 0 <= days <= MAX_FLEX_DAYS:
        raise ValueError(f"days either side must be from 0 to {MAX_FLEX_DAYS}")
    for names in (origins, destinations):
        if names:
            expand(names)


def scoped(watch: Watch, *, days: int, origins: str | None, destinations: str | None) -> Watch:
    """The watch as it would be with this scope. Nothing is saved. Raises ValueError."""
    if not 0 <= days <= MAX_FLEX_DAYS:
        raise ValueError(f"days either side must be from 0 to {MAX_FLEX_DAYS}")
    other_origins = tuple(a for a in expand(origins) if a != watch.origin) if origins else ()
    other_dests = (
        tuple(a for a in expand(destinations) if a != watch.destination) if destinations else ()
    )
    return replace(watch, flex_days=days, flex_origins=other_origins, flex_destinations=other_dests)


def review_text(watch: Watch) -> str:
    """What a scope means, in plain words, for the person to agree to."""
    if not watch.is_flexible:
        return f"{watch.label}: only the route and dates you asked for, no nearby options."
    lines = [f"{watch.label}, plus any nonstop that fits the same cabin and price rules:"]
    if watch.flex_days:
        lines.append(f"  - on dates up to {watch.flex_days} day(s) either side of yours")
    if watch.flex_origins:
        lines.append(f"  - leaving from {', '.join(watch.flex_origins)} as well as {watch.origin}")
    if watch.flex_destinations:
        lines.append(
            f"  - arriving at {', '.join(watch.flex_destinations)} as well as {watch.destination}"
        )
    total = count_scouts(watch)
    capped = total >= MAX_SCOUT_QUERIES
    lines.append(
        f"That's {'at least ' if capped else ''}{total} extra searches per sweep"
        + (
            f", and only the {MAX_SCOUT_QUERIES} closest to your plan are looked at"
            if capped
            else ""
        )
        + ". They only use searches left over after your own dates, so they never slow those "
        "down, and each place is looked at about every 6 hours. You get an urgent alert, after a "
        "live re-check, when one opens. Connections never count."
    )
    return "\n".join(lines)


def count_scouts(watch: Watch) -> int:
    """How many searches the scope adds for the dates still ahead, for the review to say."""
    return len(scout_queries(watch, date.min, lambda day: True))


def why_similar(watch: Watch, query: Query) -> str:
    """Why this search counts as similar, in words, for the alert."""
    parts = []
    nearest = min(watch.dates, key=lambda d: abs((d - query.date).days))
    shift = (query.date - nearest).days
    if shift:
        n = abs(shift)
        parts.append(f"{n} day{'s' if n != 1 else ''} {'later' if shift > 0 else 'earlier'}")
    if query.origin != watch.origin:
        parts.append(f"from {query.origin} instead of {watch.origin}")
    if query.destination != watch.destination:
        parts.append(f"to {query.destination} instead of {watch.destination}")
    return ", ".join(parts)
