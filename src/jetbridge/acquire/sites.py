"""The airline sites jetbridge can search. Adding one means adding it here."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from jetbridge.acquire import american, delta, united
from jetbridge.acquire.pagefetch import Gate
from jetbridge.acquire.types import RawResponse
from jetbridge.models import ParsedResponse, Query


@dataclass(frozen=True)
class Site:
    id: str
    label: str
    domain: str
    warmup_url: str
    markers: tuple[str, ...]  # a genuine answer contains at least one of these
    fetch: Callable[[Any, Query, Gate], Awaitable[RawResponse]]
    parse: Callable[[str], ParsedResponse]
    search_url: Callable[[Query], str]
    # A second way to get the answer when the first returned an unexpected page.
    fallback: Callable[[Any, Query], Awaitable[RawResponse]] | None = None


SITES: dict[str, Site] = {
    site.id: site
    for site in (
        Site(
            id=american.SITE,
            label="American AAdvantage",
            domain="aa.com",
            warmup_url=american.WARMUP_URL,
            markers=(american.MARKER,),
            fetch=american.fetch,
            parse=american.parse,
            search_url=american.search_url,
            fallback=american.fetch_navigate,
        ),
        Site(
            id=delta.SITE,
            label="Delta SkyMiles",
            domain="delta.com",
            warmup_url=delta.WARMUP_URL,
            markers=delta.MARKERS,
            fetch=delta.fetch,
            parse=delta.parse,
            search_url=delta.search_url,
        ),
        Site(
            id=united.SITE,
            label="United MileagePlus",
            domain="united.com",
            warmup_url=united.WARMUP_URL,
            markers=(united.MARKER,),
            fetch=united.fetch,
            parse=united.parse,
            search_url=united.search_url,
        ),
    )
}
PROGRAMS = tuple(SITES)
_ALIASES = {
    "aa": "american",
    "aadvantage": "american",
    "american airlines": "american",
    "dl": "delta",
    "skymiles": "delta",
    "delta air lines": "delta",
    "ua": "united",
    "mileageplus": "united",
    "united airlines": "united",
}


def get_site(program: str) -> Site:
    return SITES[program]


def parse_program(text: str) -> str:
    """Which program someone meant, by name or common nickname."""
    name = " ".join(text.lower().split()).removesuffix(" miles")
    name = _ALIASES.get(name, name)
    if name not in SITES:
        raise ValueError(f"please choose {', '.join(PROGRAMS[:-1])} or {PROGRAMS[-1]}")
    return name
