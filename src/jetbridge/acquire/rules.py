"""What a seat alert says about cancelling an award.

A rule is only stated here once it's been read on the airline's own page. Otherwise the alert just
points to the airline. Rules change, so one that hasn't been checked in a year stops being shown.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

MAX_AGE_DAYS = 365


@dataclass(frozen=True)
class CancelRules:
    name: str
    url: str
    checked: date
    summary: str | None = None  # None means it couldn't be confirmed on the airline's own page


CANCEL_RULES = {
    "american": CancelRules(
        "American",
        "https://www.aa.com/web/i18n/aadvantage-program/answers-support/using-miles-for-travel.html",
        date(2026, 11, 3),
    ),
    "delta": CancelRules(
        "Delta",
        "https://www.delta.com/us/en/skymiles/program-resources/program-rules",
        date(2026, 11, 3),
        "Delta's rules dated Jan 1, 2026 say most awards from the US or Canada can be changed or "
        "cancelled with no redeposit or change fee. Basic fares and other origins can differ, and "
        "any difference in miles and taxes still applies.",
    ),
    "united": CancelRules("United", "https://www.united.com", date(2026, 11, 3)),
}


def cancel_note(program: str, today: date) -> str:
    rules = CANCEL_RULES[program]
    fresh = (today - rules.checked).days <= MAX_AGE_DAYS
    if rules.summary and fresh:
        return f"If plans change: {rules.summary} Check before you book: {rules.url}"
    return (
        f"If plans change: check {rules.name}'s current cancel rules before you book: {rules.url}"
    )
