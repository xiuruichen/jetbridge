"""What a watch's own record shows. Pure functions that report what was seen, and never predict."""

from __future__ import annotations

from dataclasses import dataclass


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
