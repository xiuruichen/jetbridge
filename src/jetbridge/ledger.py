"""A ledger of what every search showed, kept only when something changed.

Each search already returns every flight and cabin on a route and date. jetbridge used to keep one
line per watch check and throw the rest away. This keeps a snapshot per route and date, and writes
an event when a flight's cabin appears, vanishes or changes price, so the record shows when each
specific flight tends to release seats. It costs no extra searches.
"""

from __future__ import annotations

from jetbridge.matcher import itinerary_key
from jetbridge.models import ParsedResponse

Offers = dict[tuple[str, str], tuple[int, int]]  # (flight or itinerary, cabin) -> (miles, seats)


def complete(parsed: ParsedResponse | None) -> bool:
    """Only a whole, readable answer can say what's gone. One with an error or a slice that couldn't
    be read might be hiding flights, and treating it as a full snapshot would invent vanishings."""
    return parsed is not None and not parsed.site_error and not parsed.unreadable


def offers_in(parsed: ParsedResponse) -> Offers:
    """The cheapest bookable offer for each flight (or connection) and cabin in this answer."""
    found: Offers = {}
    for item in parsed.slices:
        key = itinerary_key(item)
        for offer in item.offers:
            slot = (key, offer.cabin.value)
            seats = offer.seats_remaining or offer.seats_at_least
            if slot not in found or offer.miles < found[slot][0]:
                found[slot] = (offer.miles, seats)
    return found
