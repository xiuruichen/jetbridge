"""Reduce a search answer to one result for one watched flight. Pure functions only.

The rule that matters most: only a positively recognized answer can mean "no seats".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime

from jetbridge.models import (
    AnswerKind,
    CheckResult,
    CheckStatus,
    Flight,
    Leg,
    ParsedSlice,
    Query,
    SearchAnswer,
    Watch,
)


def _is_watched_itinerary(legs: tuple[Leg, ...], watch: Watch, day: date) -> bool:
    """True only for the exact flight between the watched airports, on the watched date.

    Every leg must carry the flight number, which allows a same-number technical stop but
    excludes connections. The date is the local departure date.
    """
    if not legs or any(leg.code != watch.flight.code for leg in legs):
        return False
    return (
        legs[0].origin == watch.origin
        and legs[-1].destination == watch.destination
        and legs[0].departs_at.date() == day
    )


def _could_be_watched(hints: frozenset[str], watch: Watch) -> bool:
    return not hints or watch.flight.code in hints


def is_single_flight(item: ParsedSlice, query: Query) -> bool:
    """One flight number the whole way, which is the only thing a watch can match."""
    legs = item.legs
    return (
        bool(legs)
        and all(leg.code == legs[0].code for leg in legs)
        and legs[0].origin == query.origin
        and legs[-1].destination == query.destination
        and legs[0].departs_at.date() == query.date
    )


@dataclass(frozen=True)
class AlsoSeen:
    """Another flight in the same answer that this watch's rules would have counted as open."""

    flight: str
    departs: datetime
    miles: int | None


def also_seen(watch: Watch, query: Query, answer: SearchAnswer) -> list[AlsoSeen]:
    """Other flights with qualifying seats on this route and date. These come from an answer that
    was already fetched, so looking costs no searches."""
    if answer.kind is not AnswerKind.ANSWER or answer.parsed is None or answer.parsed.site_error:
        return []
    found: dict[str, AlsoSeen] = {}
    for item in answer.parsed.slices:
        if not is_single_flight(item, query) or item.legs[0].code == watch.flight.code:
            continue
        first = item.legs[0]
        other = replace(watch, flight=Flight(first.carrier, first.number))
        result = evaluate(other, query.date, answer)
        if result.status is CheckStatus.AVAILABLE and first.code not in found:
            found[first.code] = AlsoSeen(first.code, first.departs_at, result.miles)
    return sorted(found.values(), key=lambda option: option.departs)


def evaluate(watch: Watch, day: date, answer: SearchAnswer) -> CheckResult:
    if answer.kind is not AnswerKind.ANSWER or answer.parsed is None:
        return CheckResult(
            CheckStatus.UNKNOWN,
            detail=f"{answer.kind.value}: {answer.detail or 'no detail'}",
            suspect_parser=answer.kind is AnswerKind.DRIFT,
        )

    parsed = answer.parsed
    if parsed.site_error:
        return CheckResult(CheckStatus.UNKNOWN, detail=f"site error code {parsed.site_error!r}")

    # A slice we couldn't read might be the watched flight, so "no seats" can't be claimed.
    for broken in parsed.unreadable:
        if _could_be_watched(broken.flight_hints, watch):
            return CheckResult(
                CheckStatus.UNKNOWN,
                detail=f"unreadable result that may be this flight: {broken.reason}",
                suspect_parser=True,
            )

    matches: list[ParsedSlice] = [
        s for s in parsed.slices if _is_watched_itinerary(s.legs, watch, day)
    ]
    if not matches:
        return CheckResult(CheckStatus.NOT_LISTED, detail="flight not in results")

    qualifying = [
        offer
        for s in matches
        for offer in s.offers
        if offer.cabin is watch.cabin
        and len(offer.leg_cabins) == len(s.legs)
        and all(cabin is watch.cabin for cabin in offer.leg_cabins)
    ]
    if not qualifying:
        # An unrecognized fare could be in this cabin, so "no seats" can't be claimed.
        unknown = sorted({fare for s in matches for fare in s.unknown_fares})
        if unknown:
            return CheckResult(
                CheckStatus.UNKNOWN,
                detail=f"unrecognized fare type: {', '.join(unknown)}",
                suspect_parser=True,
            )
        return CheckResult(CheckStatus.NOT_OFFERED, detail=f"no {watch.cabin.value} seats")

    best = min(qualifying, key=lambda o: o.miles)
    if watch.max_miles is not None and best.miles > watch.max_miles:
        return CheckResult(
            CheckStatus.NOT_OFFERED,
            # The price is kept for the record. These seats still don't count as open.
            miles=best.miles,
            taxes=best.taxes,
            currency=best.currency,
            detail=f"cheapest is {best.miles:,} miles, above your limit of {watch.max_miles:,}",
        )

    # The search was for this party, so the site is offering at least that many seats.
    exact = max(best.seats_remaining, watch.pax) if best.seats_remaining > 0 else None
    return CheckResult(
        CheckStatus.AVAILABLE,
        miles=best.miles,
        taxes=best.taxes,
        currency=best.currency,
        seats_exact=exact,
        seats_at_least=None if exact is not None else max(best.seats_at_least, watch.pax),
        notes=best.notes,
    )
