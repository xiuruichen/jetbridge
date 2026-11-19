"""What a watch's own record shows. Pure functions that report what was seen, and never predict."""

from __future__ import annotations

from dataclasses import dataclass

TOO_FEW_ANSWERS = 6
_OPEN = "available"
_UNKNOWN = "unknown"


@dataclass(frozen=True)
class Coverage:
    """How much of a period a date stayed on its normal rhythm of clear answers."""

    watched_s: float
    period_s: float
    longest_gap_s: float

    @property
    def share(self) -> float:
        return self.watched_s / self.period_s

    @property
    def text(self) -> str:
        return (
            f"watched {self.watched_s / 3600:.0f} of the last {self.period_s / 3600:.0f} hours, "
            f"longest gap between clear answers {self.longest_gap_s / 3600:.1f} hours"
        )


def coverage(
    clear_at: list[float], start: float, end: float, normal_gap_s: float
) -> Coverage | None:
    """Time counts as watched until the normal gap after a clear answer runs out.

    ``clear_at`` may include the last clear answer before ``start``, which still covers the start
    of the period. Returns None for a period too short to mean anything.
    """
    if end - start < 3600:
        return None
    times = sorted(t for t in clear_at if t <= end)
    before = [t for t in times if t < start]
    points = [*before[-1:], *(t for t in times if t >= start)]
    if not points:
        return Coverage(0.0, end - start, end - start)
    watched = 0.0
    longest = max(0.0, points[0] - start)
    for at, following in zip(points, [*points[1:], end], strict=True):
        watched += max(0.0, min(following, at + normal_gap_s) - max(at, start))
        longest = max(longest, following - at)
    return Coverage(watched, end - start, longest)


@dataclass(frozen=True)
class Seen:
    """One recorded answer for one date."""

    at: float
    status: str
    miles: int | None = None


@dataclass(frozen=True)
class Opening:
    """A run of answers that showed seats. Checks are hours apart, so when it really started and
    stopped is only known to within the gaps around it."""

    first_seen: float
    last_seen: float
    clear_before: float | None  # the last answer before it that showed no seats
    closed_seen: float | None  # the first answer after it that showed no seats
    lowest_miles: int | None
    highest_miles: int | None

    def lasted(self, now: float) -> str:
        at_least = self.last_seen - self.first_seen
        if self.closed_seen is None:
            return f"open for at least {_duration(max(at_least, now - self.first_seen))} so far"
        if at_least < 600 and self.clear_before is not None:
            return f"gone within {_duration(self.closed_seen - self.first_seen)} of being seen"
        if self.clear_before is None:
            return f"open for at least {_duration(at_least)}"
        at_most = self.closed_seen - self.clear_before
        return f"open for between {_duration(at_least)} and {_duration(at_most)}"


def _duration(seconds: float) -> str:
    minutes = round(seconds / 60)
    if minutes < 90:
        return f"{minutes} minute{'' if minutes == 1 else 's'}"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"{hours} hours"
    return f"{round(seconds / 86400)} days"


def openings(history: list[Seen]) -> list[Opening]:
    """The runs of seats in a date's history, oldest first. ``unknown`` answers say nothing either
    way, so they neither end a run nor start one."""
    found: list[Opening] = []
    current: list[Seen] = []
    clear_before: float | None = None
    for seen in sorted(history, key=lambda s: s.at):
        if seen.status == _UNKNOWN:
            continue
        if seen.status == _OPEN:
            current.append(seen)
            continue
        if current:
            found.append(_opening(current, clear_before, seen.at))
            current = []
        clear_before = seen.at
    if current:
        found.append(_opening(current, clear_before, None))
    return found


def _opening(run: list[Seen], clear_before: float | None, closed_seen: float | None) -> Opening:
    prices = [seen.miles for seen in run if seen.miles is not None]
    return Opening(
        run[0].at,
        run[-1].at,
        clear_before,
        closed_seen,
        min(prices, default=None),
        max(prices, default=None),
    )


def clear_answers(history: list[Seen]) -> int:
    return sum(seen.status != _UNKNOWN for seen in history)


CLOSING_SOON_SHARE = 0.75
CLOSING_SOON_MIN_S = 1800.0


def _gone_within(found: Opening) -> float:
    """The longest a finished opening can have lasted, going by the answers around it."""
    start = found.clear_before if found.clear_before is not None else found.first_seen
    return found.closed_seen - start  # type: ignore[operator]


def _past_range(past: list[Opening]) -> str:
    shortest = _duration(min(_gone_within(found) for found in past))
    longest = _duration(max(_gone_within(found) for found in past))
    return f"within {shortest}" if shortest == longest else f"within {shortest} to {longest}"


def hold_note(history: list[Seen]) -> str | None:
    """How fast this date's past openings closed, so someone can decide how long to think. Only what
    was seen: it says nothing about this opening."""
    past = [found for found in openings(history) if found.closed_seen is not None]
    if not past:
        return None
    which = "The last opening" if len(past) == 1 else f"The {len(past)} past openings"
    return f"{which} on this date {'was' if len(past) == 1 else 'were'} gone {_past_range(past)}."


def closing_soon(history: list[Seen], now: float) -> tuple[float, str] | None:
    """For seats that are open now: when this opening started, and a line to say it has been open
    about as long as the shortest past one was known to last. None until that point, and when
    there's no past opening to compare with."""
    runs = openings(history)
    if not runs or runs[-1].closed_seen is not None:
        return None
    current, past = runs[-1], [found for found in runs[:-1] if found.closed_seen is not None]
    if not past:
        return None
    threshold = max(
        CLOSING_SOON_MIN_S, CLOSING_SOON_SHARE * min(_gone_within(found) for found in past)
    )
    open_for = now - current.first_seen
    if open_for < threshold:
        return None
    note = (
        f"These seats have now been open for at least {_duration(open_for)}. Past openings on this "
        f"date were gone {_past_range(past)}."
    )
    return current.first_seen, note


def alert_note(history: list[Seen], now: float) -> str | None:
    """A line for a seats alert saying what this date has done before. Past openings only, so the
    one being announced doesn't count. Says nothing rather than read much into very little."""
    past = [found for found in openings(history) if found.closed_seen is not None]
    if past:
        times = "once" if len(past) == 1 else f"{len(past)} times"
        return f"Seen open {times} before on this date, most recently {past[-1].lasted(now)}."
    count = clear_answers(history)
    if count >= TOO_FEW_ANSWERS:
        return f"The first seats jetbridge has seen on this date, in {count} clear answers."
    return None
