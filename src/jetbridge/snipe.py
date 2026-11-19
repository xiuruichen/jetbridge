"""Snipe windows: check one watch's dates about every 15 minutes inside a chosen time of day.

Seats that open and close within hours are easy to miss at a pace of a few checks a day. A window
spends searches where they have the best chance, and nowhere else. It is the most likely thing to
get a site to block, so it is opt-in, capped, and limited to a few watches. It only ever uses
searches left over from the day's plan.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from jetbridge.models import Watch
from jetbridge.predictor import Pattern
from jetbridge.store import Store

SNIPE_GAP_S = 840.0  # the watcher wakes about every 15 minutes, so this is every wake-up
DAILY_CAP = 36  # searches per watch per day
MAX_WATCHES = 3
MAX_WINDOW_MIN = 6 * 60
AUTO = "auto"  # use the time of day learned from this computer's own openings

_WINDOW = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*-\s*(\d{1,2}):(\d{2})\s*$")


def parse_window(text: str) -> str:
    """'2:00-5:00' becomes '02:00-05:00'. Raises ValueError for anything unclear or too long."""
    if text.strip().lower() == AUTO:
        return AUTO
    match = _WINDOW.match(text)
    if match is None:
        raise ValueError("give a window like 02:00-05:00 in your computer's local time, or auto")
    sh, sm, eh, em = (int(part) for part in match.groups())
    if sh > 23 or eh > 23 or sm > 59 or em > 59:
        raise ValueError("that isn't a time of day: use 00:00 to 23:59")
    start, end = sh * 60 + sm, eh * 60 + em
    length = (end - start) % (24 * 60)
    if length == 0 or length > MAX_WINDOW_MIN:
        raise ValueError(f"a window has to be from 1 minute to {MAX_WINDOW_MIN // 60} hours long")
    return f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"


def minutes(window: str) -> tuple[int, int]:
    """Start and end as minutes after midnight. The end may be earlier: it wraps midnight."""
    match = _WINDOW.match(window)
    assert match is not None, window
    sh, sm, eh, em = (int(part) for part in match.groups())
    return sh * 60 + sm, eh * 60 + em


def is_inside(bounds: tuple[int, int], now: float) -> bool:
    start, end = bounds
    moment = datetime.fromtimestamp(now)
    minute = moment.hour * 60 + moment.minute
    return start <= minute < end if start < end else minute >= start or minute < end


def window_day(bounds: tuple[int, int], now: float) -> date:
    """The day a window belongs to, so one that crosses midnight spends one day's cap, not two."""
    moment = datetime.fromtimestamp(now)
    start, end = bounds
    if start > end and moment.hour * 60 + moment.minute < end:
        return moment.date() - timedelta(days=1)
    return moment.date()


def next_start(bounds: tuple[int, int], now: float) -> float:
    """The next time this window opens, as an epoch in the computer's local time."""
    moment = datetime.fromtimestamp(now)
    start = moment.replace(hour=bounds[0] // 60, minute=bounds[0] % 60, second=0, microsecond=0)
    if start <= moment:
        start += timedelta(days=1)
    return start.timestamp()


def describe(window: str, learned: str | None = None) -> str:
    if window == AUTO:
        return (
            f"the time of day learned from your own openings ({learned})"
            if learned
            else ("the time of day learned from your own openings, once enough are recorded")
        )
    return f"{window} your computer's time"


def review_text(watch: Watch, window: str | None, learned: str | None = None) -> str:
    """What a window means, in plain words, for the person to agree to."""
    if window is None:
        return f"{watch.label}: no snipe window, so its dates keep their normal pace."
    return (
        f"{watch.label}: inside {describe(window, learned)}, each of its dates is checked about "
        f"every 15 minutes, up to {DAILY_CAP} searches a day for this watch.\n"
        "  - Outside the window nothing changes.\n"
        "  - It only uses searches left over from the day's plan, and stops at the first sign of "
        "a block.\n"
        "  - This is the most likely thing to get a site to block you, and automated searching "
        "likely goes against the airlines' terms.\n"
        "  - Checks only run while your computer is awake. It can't promise you'll get a seat.\n"
        f"  - At most {MAX_WATCHES} watches can have one."
    )


def check(store: Store, watch: Watch, window: str | None) -> str | None:
    """The window to save, or None to turn it off. Raises ValueError if it can't be saved."""
    if window is None:
        return None
    parsed = parse_window(window)
    if store.snipe_watch_count(leaving_out=watch.id) >= MAX_WATCHES:
        raise ValueError(
            f"only {MAX_WATCHES} watches can have a snipe window at once, to protect the day's "
            "searches. Turn one off first"
        )
    return parsed


def learned_text(store: Store, program: str) -> str | None:
    """The time of day learned for this program, in words, if there is one yet."""
    saved = store.get(f"release_pattern:{program}")
    return Pattern(**json.loads(saved)).text if saved else None
