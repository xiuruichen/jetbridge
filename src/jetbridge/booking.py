"""Getting from an alert to the airline's own page. jetbridge only opens the page: it never signs
in, books, pays or moves miles, so the last steps are always yours."""

from __future__ import annotations

from datetime import date

from jetbridge.models import CheckStatus, Watch
from jetbridge.store import Store

NOTES = {
    "american": "The link goes straight to the results on aa.com.",
    "delta": "The link opens a date grid on delta.com with this date selected: press Continue.",
    "united": "united.com asks them to sign in before it shows prices in miles.",
}


def pick_day(store: Store, watch: Watch, today: date, asked: date | None = None) -> date:
    """The date to open: the one asked for, else the first with seats open, else the first ahead.

    Raises ValueError when the watch has no date to open."""
    if asked is not None:
        if asked not in watch.dates:
            raise ValueError(f"{asked:%b %d, %Y} isn't one of this watch's dates")
        return asked
    ahead = [day for day in watch.dates if day >= today]
    if not ahead:
        raise ValueError("every date on this watch has passed")
    for day in ahead:
        if store.date_state(watch.id, day).status is CheckStatus.AVAILABLE:
            return day
    return ahead[0]
