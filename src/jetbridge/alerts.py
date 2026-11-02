"""Alert rules and alert text. Pure functions of recorded state and the new result."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import date, datetime
from enum import StrEnum

from jetbridge.models import CheckResult, CheckStatus, Query, Watch

REMINDER_INTERVAL_S = 24 * 3600


class AlertKind(StrEnum):
    SEATS_OPEN = "seats_open"
    SEATS_OPEN_UNCONFIRMED = "seats_open_unconfirmed"
    PRICE_DROP = "price_drop"
    MORE_SEATS = "more_seats"
    REMINDER = "reminder"
    SEATS_GONE = "seats_gone"


@dataclass(frozen=True)
class DateState:
    """What is recorded for one watch on one date. Open/closed changes only on conclusive
    results, so an UNKNOWN can neither announce seats nor make them disappear."""

    status: CheckStatus | None = None
    miles: int | None = None
    seats_exact: int | None = None
    last_alert_at: float | None = None
    last_check_at: float | None = None
    last_conclusive_at: float | None = None
    conclusive_count: int = 0
    ever_listed: bool = False


@dataclass(frozen=True)
class Decision:
    state: DateState
    alerts: tuple[AlertKind, ...] = ()
    needs_confirmation: bool = False


def _after_conclusive(prev: DateState, result: CheckResult, now: float) -> DateState:
    return replace(
        prev,
        last_check_at=now,
        last_conclusive_at=now,
        conclusive_count=prev.conclusive_count + 1,
        ever_listed=prev.ever_listed or result.status is not CheckStatus.NOT_LISTED,
    )


def _adopt(state: DateState, result: CheckResult) -> DateState:
    available = result.status is CheckStatus.AVAILABLE
    return replace(
        state,
        status=result.status,
        miles=result.miles if available else None,
        seats_exact=result.seats_exact if available else None,
    )


def decide(prev: DateState, result: CheckResult, now: float) -> Decision:
    if result.status is CheckStatus.UNKNOWN:
        return Decision(replace(prev, last_check_at=now))

    state = _after_conclusive(prev, result, now)
    was_open = prev.status is CheckStatus.AVAILABLE
    is_open = result.status is CheckStatus.AVAILABLE

    if is_open and not was_open:
        # Status stays closed until the live re-check; a crash just re-detects it next time.
        return Decision(state, needs_confirmation=True)

    if was_open and not is_open:
        return Decision(replace(_adopt(state, result), last_alert_at=now), (AlertKind.SEATS_GONE,))

    new_state = _adopt(state, result)
    if not is_open:
        return Decision(new_state)

    alerts: list[AlertKind] = []
    if prev.miles is not None and result.miles is not None and result.miles < prev.miles:
        alerts.append(AlertKind.PRICE_DROP)
    if (
        prev.seats_exact is not None
        and result.seats_exact is not None
        and result.seats_exact > prev.seats_exact
    ):
        alerts.append(AlertKind.MORE_SEATS)
    if not alerts and (
        prev.last_alert_at is None or now - prev.last_alert_at >= REMINDER_INTERVAL_S
    ):
        alerts.append(AlertKind.REMINDER)
    if alerts:
        new_state = replace(new_state, last_alert_at=now)
    return Decision(new_state, tuple(alerts))


def confirm_opening(
    state: DateState, first: CheckResult, recheck: CheckResult, now: float
) -> Decision:
    """Resolve the live re-check that follows a change to AVAILABLE."""
    if recheck.status is CheckStatus.AVAILABLE:
        return Decision(
            replace(_adopt(_after_conclusive(state, recheck, now), recheck), last_alert_at=now),
            (AlertKind.SEATS_OPEN,),
        )
    if recheck.status is CheckStatus.UNKNOWN:
        return Decision(
            replace(_adopt(state, first), last_check_at=now, last_alert_at=now),
            (AlertKind.SEATS_OPEN_UNCONFIRMED,),
        )
    # Brief flicker: logged as an observation, no email, and the state stays closed.
    return Decision(_adopt(_after_conclusive(state, recheck, now), recheck))


# ---- text -----------------------------------------------------------------------------


def format_when(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).astimezone().strftime("%b %d, %Y %I:%M %p %Z")


def _price(result: CheckResult) -> str:
    if result.miles is None:
        return "price unknown"
    tax = f" + {result.currency} {result.taxes:,.2f}" if result.taxes is not None else ""
    return f"{result.miles:,} miles{tax} per person"


DEFAULT_HEADLINES = {
    AlertKind.SEATS_OPEN: "SEATS OPEN",
    AlertKind.SEATS_OPEN_UNCONFIRMED: "SEATS MAY BE OPEN (unconfirmed)",
    AlertKind.PRICE_DROP: "PRICE DROP",
    AlertKind.MORE_SEATS: "MORE SEATS",
    AlertKind.REMINDER: "STILL OPEN",
    AlertKind.SEATS_GONE: "Seats gone",
}


def render_availability(
    kind: AlertKind,
    watch: Watch,
    day: date,
    result: CheckResult,
    prev: DateState,
    now: float,
    booking_url: str,
    headlines: Mapping[AlertKind, str] | None = None,
) -> tuple[str, str]:
    day_text = day.strftime("%b %d, %Y")
    headline = (headlines or {}).get(kind, DEFAULT_HEADLINES[kind])
    head = f"{headline}: {watch.flight.code} {watch.cabin.value} on {day_text}"
    if kind is AlertKind.SEATS_GONE:
        lines = [
            f"{watch.label} on {day_text} no longer shows qualifying seats.",
            *([f"Last seen at: {prev.miles:,} miles."] if prev.miles else []),
            *([f"Reason: {result.detail}"] if result.detail else []),
            f"Checked: {format_when(now)}",
            "",
            "jetbridge keeps watching and will email you if seats come back.",
        ]
        return head, "\n".join(lines)

    subject = f"{head} - {result.miles:,} mi" if result.miles else head
    lines = [
        f"Flight:   {watch.flight.code} {watch.origin} to {watch.destination}",
        f"Date:     {day_text}",
        f"Cabin:    {watch.cabin.value}",
        f"Price:    {_price(result)}",
        f"Seats:    {result.seats_text} (searched for {watch.pax})",
        f"Program:  {watch.program}",
        f"Observed: {format_when(now)}",
    ]
    if kind is AlertKind.PRICE_DROP and prev.miles:
        lines.append(f"Was:      {prev.miles:,} miles")
    if kind is AlertKind.MORE_SEATS and prev.seats_exact:
        lines.append(f"Was:      {prev.seats_exact} seats")
    lines.extend(f"Note:     {note}" for note in result.notes)
    if kind is AlertKind.SEATS_OPEN_UNCONFIRMED:
        lines += ["", "The follow-up check was inconclusive, so this may already be gone."]
    lines += [
        "",
        "Check and book:",
        booking_url,
        "",
        "Award seats can vanish quickly. "
        "Confirm on the site before moving any points, because transfers can't be undone.",
    ]
    return subject, "\n".join(lines)


def query_for(watch: Watch, day: date) -> Query:
    return Query(watch.origin, watch.destination, day, watch.pax, watch.program)
