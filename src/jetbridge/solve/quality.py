"""Itinerary quality analysis: the facts a human expert checks before booking.

Pure functions of an itinerary (+ KB): connection times vs minimum connection
time, overnight / long layovers, airport changes, red-eyes, hours flown per
cabin, and the "comfort deficit": hours flown below the preferred cabin,
weighted by how much the traveller values that cabin per hour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta

from jetbridge.kb import KB
from jetbridge.models.core import Cabin, Itinerary
from jetbridge.models.spec import Preferences


@dataclass(frozen=True)
class Layover:
    airport: str  # arrival airport (== departure airport unless airport change)
    minutes: int
    overnight: bool
    airport_change: tuple[str, str] | None
    tight: bool  # below MCT + small margin
    separate_ticket: bool = False


@dataclass
class Quality:
    flown_hours: float
    total_hours: float
    hours_by_cabin: dict[Cabin, float]
    longhaul_cabin: Cabin
    longhaul_hours: float
    layovers: list[Layover]
    redeye: bool
    comfort_deficit_usd: float  # per pax, vs preferred cabin
    flags: list[str] = field(default_factory=list)

    @property
    def stops(self) -> int:
        return len(self.layovers)


def _is_domestic(kb: KB, a: str, b: str) -> bool:
    try:
        return kb.airport(a).country == kb.airport(b).country
    except KeyError:
        return False


def analyze(
    it: Itinerary,
    kb: KB,
    prefs: Preferences,
    preferred: Cabin,
    *,
    separate_ticket_after: set[int] | None = None,
) -> Quality:
    """``separate_ticket_after``: indices i where segment i+1 is on a different ticket."""
    sep = separate_ticket_after or set()
    hours_by_cabin: dict[Cabin, float] = {}
    for s in it.segments:
        hours_by_cabin[s.cabin] = (
            hours_by_cabin.get(s.cabin, 0.0) + s.duration.total_seconds() / 3600
        )
    longest = max(it.segments, key=lambda s: s.duration)
    want = prefs.cabin_value(preferred)
    deficit = sum(
        s.duration.total_seconds() / 3600 * max(0.0, want - prefs.cabin_value(s.cabin))
        for s in it.segments
    )
    layovers: list[Layover] = []
    flags: list[str] = []
    for i, (a, b) in enumerate(zip(it.segments, it.segments[1:], strict=False)):
        gap = b.departs_at - a.arrives_at
        mins = int(gap.total_seconds() // 60)
        change = (a.destination, b.origin) if a.destination != b.origin else None
        local_arr = a.arrives_at
        # overnight: layover covering 01:00-05:00 local, or >= 8h
        overnight = gap >= timedelta(hours=8) or (
            gap >= timedelta(hours=4)
            and (local_arr.hour >= 22 or local_arr.hour < 4)
            and (local_arr + gap).hour >= 5
        )
        intl = not (
            _is_domestic(kb, a.origin, a.destination) and _is_domestic(kb, b.origin, b.destination)
        )
        mct = kb.mct_minutes("default_international" if intl else "default_domestic")
        if change:
            mct = max(mct, kb.mct_minutes("airport_change_buffer"))
        is_sep = i in sep
        if is_sep:
            mct = max(mct, kb.mct_minutes("separate_ticket_buffer"))
        tight = mins < mct + (0 if is_sep else 15)
        layovers.append(Layover(a.destination, mins, overnight, change, tight, is_sep))
        if tight:
            flags.append(
                f"tight connection at {a.destination}: {mins} min"
                + (" on separate tickets" if is_sep else "")
                + f" (guideline {mct} min)"
            )
        if overnight:
            flags.append(f"overnight layover at {a.destination} ({mins // 60}h{mins % 60:02d}m)")
        elif mins > prefs.long_layover_hours * 60:
            flags.append(f"long layover at {a.destination} ({mins // 60}h{mins % 60:02d}m)")
        if change:
            flags.append(f"airport change {change[0]}→{change[1]} ({mins} min)")
    redeye = any(
        s.duration >= timedelta(hours=4)
        and (s.departs_at.hour >= 21 or s.departs_at.hour < 3)
        and s.arrives_at.hour < 9
        for s in it.segments
    )
    return Quality(
        flown_hours=sum(hours_by_cabin.values()),
        total_hours=it.duration.total_seconds() / 3600,
        hours_by_cabin=hours_by_cabin,
        longhaul_cabin=longest.cabin,
        longhaul_hours=longest.duration.total_seconds() / 3600,
        layovers=layovers,
        redeye=redeye,
        comfort_deficit_usd=deficit,
        flags=flags,
    )


def penalty_usd(
    q: Quality, prefs: Preferences, dest_weight: float, dest_weight_usd: float = 800
) -> float:
    """Per-passenger disutility in USD (lower is better), excluding price."""
    p = q.comfort_deficit_usd
    p += q.total_hours * prefs.value_of_time_usd_per_hour
    p += q.stops * prefs.stop_penalty_usd
    p += sum(prefs.overnight_layover_penalty_usd for lv in q.layovers if lv.overnight)
    p += sum(prefs.airport_change_penalty_usd for lv in q.layovers if lv.airport_change)
    p += sum(prefs.separate_ticket_penalty_usd for lv in q.layovers if lv.separate_ticket)
    p += (1 - dest_weight) * dest_weight_usd
    return p
