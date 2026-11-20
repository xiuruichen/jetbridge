"""Reduce a search answer to one result for one watched flight. Pure functions only.

The rule that matters most: only a positively recognized answer can mean "no seats".
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime
from itertools import pairwise

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

SO_FAR = "seats for "  # starts the answer when a split party is only partly seated


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
    return not hints or watch.flight.is_any or watch.flight.code in hints


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
    via: str = ""  # for a connection: where it connects, and for how long


def itinerary_key(item: ParsedSlice) -> str:
    """AA100 for a nonstop, AA100+BA15 for a connection."""
    return "+".join(leg.code for leg in item.legs)


def _hours(start: datetime, end: datetime) -> float | None:
    try:
        return (end - start).total_seconds() / 3600
    except TypeError:  # one time has a zone and the other doesn't, so it can't be compared
        return None


def _duration_text(hours: float) -> str:
    whole, minutes = divmod(round(hours * 60), 60)
    return f"{whole}h{minutes:02d}" if minutes else f"{whole}h"


def connection_ok(item: ParsedSlice, query: Query, watch: Watch) -> bool:
    """A one-ticket itinerary from the asked airport to the asked airport, on the asked date, with
    no more stops, layover or total time than this watch allows. Times that can't be compared
    count against it, because a limit can't be claimed met when it can't be checked."""
    legs = item.legs
    if not 1 < len(legs) <= watch.max_stops + 1:
        return False
    if legs[0].origin != query.origin or legs[-1].destination != query.destination:
        return False
    if legs[0].departs_at.date() != query.date:
        return False
    for before, after in pairwise(legs):
        if before.destination != after.origin:
            return False
        layover = _hours(before.arrives_at, after.departs_at)
        if layover is None or not 0 <= layover <= watch.max_layover_hours:
            return False
    total = _hours(legs[0].departs_at, legs[-1].arrives_at)
    return total is not None and (watch.max_total_hours is None or total <= watch.max_total_hours)


def via_text(item: ParsedSlice) -> str:
    """Where a connection connects and how long it takes, or nothing for a nonstop."""
    legs = item.legs
    if len(legs) < 2:
        return ""
    stops = ", ".join(leg.destination for leg in legs[:-1])
    layovers = [_hours(a.arrives_at, b.departs_at) for a, b in pairwise(legs)]
    waits = ", ".join(_duration_text(w) for w in layovers if w is not None)
    total = _hours(legs[0].departs_at, legs[-1].arrives_at)
    parts = [f"via {stops}"]
    if waits:
        parts.append(f"{waits} layover")
    if total is not None:
        parts.append(f"{_duration_text(total)} total")
    return ", ".join(parts)


def _connection_notes(matches: list[ParsedSlice]) -> tuple[str, ...]:
    if len(matches) == 1 and len(matches[0].legs) > 1:
        return (f"One-stop itinerary on a single ticket, {via_text(matches[0])}.",)
    return ()


def also_seen(watch: Watch, query: Query, answer: SearchAnswer) -> list[AlsoSeen]:
    """Other flights with qualifying seats on this route and date. These come from an answer that
    was already fetched, so looking costs no searches."""
    if answer.kind is not AnswerKind.ANSWER or answer.parsed is None or answer.parsed.site_error:
        return []
    if watch.flight.is_any:  # it's already watching all of these
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


def open_nonstops(watch: Watch, query: Query, answer: SearchAnswer) -> list[AlsoSeen]:
    """Every nonstop with seats that fit this watch's rules, for scouting a nearby date or airport.
    The answer was already fetched, so looking costs no searches."""
    if answer.kind is not AnswerKind.ANSWER or answer.parsed is None or answer.parsed.site_error:
        return []
    found: dict[str, AlsoSeen] = {}
    for item in answer.parsed.slices:
        first = item.legs[0] if item.legs else None
        if first is None:
            continue
        if is_single_flight(item, query) and len(item.legs) == 1:
            other = replace(watch, flight=Flight(first.carrier, first.number))
            result = evaluate(other, query.date, answer)
            key, via = first.code, ""
        elif watch.max_stops > 0 and connection_ok(item, query, watch):
            result = _judge(watch, [item])
            key, via = itinerary_key(item), via_text(item)
        else:
            continue
        if result.status is CheckStatus.AVAILABLE and key not in found:
            found[key] = AlsoSeen(key, first.departs_at, result.miles, via)
    return sorted(found.values(), key=lambda option: option.departs)


def _distinct_seats(available: dict[str, CheckResult], pax: int) -> int:
    """Seats that can be told apart. A nonstop and a connection on the same flight draw on the same
    seats, so only itineraries sharing no flight number are added together, most seats first."""
    used: set[str] = set()
    total = 0
    ranked = sorted(
        available.items(),
        key=lambda kv: (-(kv[1].seats_exact or kv[1].seats_at_least or pax), kv[0]),
    )
    for key, result in ranked:
        codes = set(key.split("+"))
        if codes & used:
            continue
        used |= codes
        total += result.seats_exact or result.seats_at_least or pax
    return total


def _evaluate_any(watch: Watch, day: date, answer: SearchAnswer) -> CheckResult:
    """Judge each nonstop flight on the route by the same rules as a watch on that flight. Someone
    chose this in the review, and a connection still never counts."""
    parsed = answer.parsed
    assert parsed is not None
    query = Query(watch.origin, watch.destination, day, watch.pax, watch.program)
    results: dict[str, CheckResult] = {}
    for item in parsed.slices:
        if is_single_flight(item, query) and len(item.legs) == 1:
            leg = item.legs[0]
            exact = replace(watch, flight=Flight(leg.carrier, leg.number))
            results.setdefault(leg.code, evaluate(exact, day, answer))
        elif watch.max_stops > 0 and connection_ok(item, query, watch):
            results.setdefault(itinerary_key(item), _judge(watch, [item]))

    available = {
        code: result for code, result in results.items() if result.status is CheckStatus.AVAILABLE
    }
    seats = _distinct_seats(available, watch.pax)
    if available and (not watch.whole_party or seats >= watch.whole_party):
        code = min(available, key=lambda c: available[c].miles or 0)
        return replace(available[code], flight=code, flights=tuple(sorted(available)))
    # A slice that couldn't be read might be a nonstop with seats, so "no seats" can't be claimed.
    if parsed.unreadable:
        reason = parsed.unreadable[0].reason
        return CheckResult(
            CheckStatus.UNKNOWN,
            detail=f"unreadable result that may be a nonstop: {reason}",
            suspect_parser=True,
        )
    unclear = [r for r in results.values() if r.status is CheckStatus.UNKNOWN]
    if unclear:
        return unclear[0]
    if available:  # open, but not enough of it to seat everyone, so it doesn't count yet
        names = ", ".join(sorted(available))
        return CheckResult(
            CheckStatus.NOT_OFFERED,
            detail=f"{SO_FAR}{seats} of your {watch.whole_party} so far, on {names}",
        )
    if not results:
        none = "no flights" if watch.max_stops else "no nonstop flights"
        return CheckResult(CheckStatus.NOT_LISTED, detail=f"{none} in results")
    over = [r for r in results.values() if r.miles]
    if over:
        return min(over, key=lambda r: r.miles or 0)
    return CheckResult(CheckStatus.NOT_OFFERED, detail=f"no {watch.cabin.value} seats")


def _judge(watch: Watch, matches: list[ParsedSlice]) -> CheckResult:
    """Judge these itineraries, already known to be the right ones, by the watch's cabin, price and
    party rules. A connection says so in the result's notes."""
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
    limit = watch.price_limit
    if limit is not None and best.miles > limit:
        detail = (
            f"cheapest is {best.miles:,} miles, not below the {watch.booked_miles:,} you paid"
            if watch.booked_miles
            else f"cheapest is {best.miles:,} miles, above your limit of {limit:,}"
        )
        return CheckResult(
            CheckStatus.NOT_OFFERED,
            # The price is kept for the record. These seats still don't count as open.
            miles=best.miles,
            taxes=best.taxes,
            currency=best.currency,
            detail=detail,
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
        notes=best.notes + _connection_notes(matches),
    )


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
    if watch.flight.is_any:
        return _evaluate_any(watch, day, answer)

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

    return _judge(watch, matches)
