"""When seats on this computer's own watches have first shown up, if that clusters at a time of day.

It learns only from this watch history and never leaves this computer. It says nothing until there
are enough openings with a tight enough record, and a pattern from a handful of openings can be
chance, so it's used only to time one extra check a day, within the same search limits.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from jetbridge.insights import Seen, openings

MIN_OPENINGS = 5
WINDOW_HOURS = 3
MIN_SHARE = 0.6
TIGHT_S = 3 * 3600.0  # the answer before the opening has to be this close, or its start is a guess


@dataclass(frozen=True)
class Pattern:
    start_hour: int  # local hour the window opens, 0-23
    count: int  # openings inside the window
    total: int  # openings the record could place

    @property
    def share(self) -> float:
        return self.count / self.total

    @property
    def text(self) -> str:
        end = (self.start_hour + WINDOW_HOURS) % 24
        return (
            f"{self.count} of {self.total} past openings were first seen between "
            f"{_clock(self.start_hour)} and {_clock(end)} your time"
        )

    def next_start(self, now: float) -> float:
        """The next time the window opens, as an epoch, from the computer's local time."""
        moment = datetime.fromtimestamp(now)
        start = moment.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        if start <= moment:
            start += timedelta(days=1)
        return start.timestamp()

    def is_inside(self, now: float) -> bool:
        hour = datetime.fromtimestamp(now).hour
        return (hour - self.start_hour) % 24 < WINDOW_HOURS

    def window_start_today(self, now: float) -> float:
        """When the window that's open at ``now`` began."""
        moment = datetime.fromtimestamp(now)
        start = moment.replace(hour=self.start_hour, minute=0, second=0, microsecond=0)
        if start > moment:
            start -= timedelta(days=1)
        return start.timestamp()


def _clock(hour: int) -> str:
    suffix = "AM" if hour < 12 else "PM"
    return f"{hour % 12 or 12} {suffix}"


def first_seen_times(histories: list[list[Seen]]) -> list[float]:
    """When each opening was first seen, for openings whose start is known to within a few hours:
    the answer before it must have shown no seats, and been recent."""
    times = []
    for history in histories:
        for found in openings(history):
            if found.clear_before is not None and found.first_seen - found.clear_before <= TIGHT_S:
                times.append(found.first_seen)
    return times


def flight_patterns(events: list[tuple[str, str, float]]) -> dict[tuple[str, str], Pattern]:
    """A release window for each flight and cabin with enough openings, from the ledger's events."""
    grouped: dict[tuple[str, str], list[float]] = {}
    for flight, cabin, at in events:
        grouped.setdefault((flight, cabin), []).append(at)
    found = {key: find_pattern(times) for key, times in grouped.items()}
    return {key: pattern for key, pattern in found.items() if pattern is not None}


def find_pattern(first_seen: list[float]) -> Pattern | None:
    """The 3-hour window of the day holding most openings, if there are enough and it holds most."""
    if len(first_seen) < MIN_OPENINGS:
        return None
    hours = [datetime.fromtimestamp(at).hour for at in first_seen]
    best_start, best_count = 0, -1
    for start in range(24):
        count = sum((hour - start) % 24 < WINDOW_HOURS for hour in hours)
        if count > best_count:
            best_start, best_count = start, count
    if best_count / len(hours) < MIN_SHARE:
        return None
    return Pattern(best_start, best_count, len(hours))
