"""A watch being put together, and the friendly input that fills it in.

Every way of adding a watch fills in a draft, shows the same review, and then goes through
``service.create_watch``. Nothing here guesses: anything unclear raises ``ValueError`` so the
person can be asked again.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from jetbridge import service
from jetbridge.acquire.sites import get_site
from jetbridge.models import Cabin, Watch
from jetbridge.store import Store

DEFAULT_CHECKS_PER_DAY = 6
HIGH_VOLUME_SEARCHES = 36

# Common airports, by the names people use. A city with several airports lists them all, so the
# person chooses. Anything else can be entered as a 3-letter airport code.
_CITY_AIRPORTS: dict[str, tuple[str, ...]] = {
    "amsterdam": ("AMS",),
    "athens": ("ATH",),
    "atlanta": ("ATL",),
    "auckland": ("AKL",),
    "austin": ("AUS",),
    "bangkok": ("BKK", "DMK"),
    "barcelona": ("BCN",),
    "beijing": ("PEK", "PKX"),
    "berlin": ("BER",),
    "boston": ("BOS",),
    "brussels": ("BRU",),
    "buenos aires": ("EZE", "AEP"),
    "cancun": ("CUN",),
    "charlotte": ("CLT",),
    "chicago": ("ORD", "MDW"),
    "cincinnati": ("CVG",),
    "copenhagen": ("CPH",),
    "dallas": ("DFW", "DAL"),
    "delhi": ("DEL",),
    "denver": ("DEN",),
    "detroit": ("DTW",),
    "doha": ("DOH",),
    "dubai": ("DXB",),
    "dublin": ("DUB",),
    "frankfurt": ("FRA",),
    "guangzhou": ("CAN",),
    "hanoi": ("HAN",),
    "helsinki": ("HEL",),
    "ho chi minh city": ("SGN",),
    "hong kong": ("HKG",),
    "honolulu": ("HNL",),
    "houston": ("IAH", "HOU"),
    "istanbul": ("IST", "SAW"),
    "jakarta": ("CGK",),
    "johannesburg": ("JNB",),
    "kuala lumpur": ("KUL",),
    "las vegas": ("LAS",),
    "lima": ("LIM",),
    "lisbon": ("LIS",),
    "london": ("LHR", "LGW", "LCY", "STN"),
    "los angeles": ("LAX",),
    "madrid": ("MAD",),
    "manila": ("MNL",),
    "melbourne": ("MEL",),
    "mexico city": ("MEX", "NLU"),
    "miami": ("MIA",),
    "milan": ("MXP", "LIN"),
    "minneapolis": ("MSP",),
    "montreal": ("YUL",),
    "mumbai": ("BOM",),
    "munich": ("MUC",),
    "new york": ("JFK", "EWR", "LGA"),
    "orlando": ("MCO",),
    "osaka": ("KIX", "ITM"),
    "paris": ("CDG", "ORY"),
    "philadelphia": ("PHL",),
    "phoenix": ("PHX",),
    "rome": ("FCO",),
    "san diego": ("SAN",),
    "san francisco": ("SFO",),
    "santiago": ("SCL",),
    "sao paulo": ("GRU", "CGH"),
    "seattle": ("SEA",),
    "seoul": ("ICN", "GMP"),
    "shanghai": ("PVG", "SHA"),
    "singapore": ("SIN",),
    "stockholm": ("ARN",),
    "sydney": ("SYD",),
    "taipei": ("TPE", "TSA"),
    "tel aviv": ("TLV",),
    "tokyo": ("HND", "NRT"),
    "toronto": ("YYZ", "YTZ"),
    "vancouver": ("YVR",),
    "vienna": ("VIE",),
    "washington": ("IAD", "DCA", "BWI"),
    "zurich": ("ZRH",),
}
_CITY_NICKNAMES = {
    "dc": "washington",
    "hk": "hong kong",
    "la": "los angeles",
    "nyc": "new york",
    "saigon": "ho chi minh city",
    "sf": "san francisco",
    "washington dc": "washington",
}
_CABINS = {
    "business": Cabin.BUSINESS,
    "biz": Cabin.BUSINESS,
    "economy": Cabin.ECONOMY,
    "coach": Cabin.ECONOMY,
    "main": Cabin.ECONOMY,
    "first": Cabin.FIRST,
    "premium": Cabin.PREMIUM,
    "premium economy": Cabin.PREMIUM,
}
_MONTHS = {
    name.lower(): number
    for names in (calendar.month_name, calendar.month_abbr)
    for number, name in enumerate(names)
    if name
} | {"sept": 9}
_DATE_PART = re.compile(
    r"^(?:(?P<m1>[a-z]+)\s+)?(?P<d1>\d{1,2})(?:\s+(?P<y1>\d{4}))?"
    r"(?:\s*(?:-|–|—|\bto\b|\bthrough\b|\bthru\b|\buntil\b)\s*"
    r"(?:(?P<m2>[a-z]+)\s+)?(?P<d2>\d{1,2})(?:\s+(?P<y2>\d{4}))?)?$"
)
_MILES = re.compile(r"^(\d+(?:\.\d+)?)\s*(k?)(?:\s*(?:miles|mi))?$")


@dataclass
class WatchDraft:
    """A watch that hasn't been saved. Empty fields still need an answer."""

    origin: str | None = None
    destination: str | None = None
    dates: str | None = None  # in the form service.parse_dates accepts
    flight: str | None = None
    cabin: Cabin = Cabin.BUSINESS
    program: str = "american"
    pax: int = 1
    checks_per_day: int = DEFAULT_CHECKS_PER_DAY
    max_miles: int | None = None
    recipients: str | None = None  # None means the owner

    def missing(self) -> list[str]:
        fields = ("origin", "destination", "dates", "flight")
        return [name for name in fields if not getattr(self, name)]

    @property
    def searches_per_day(self) -> int:
        return len(service.parse_dates(self.dates or "")) * self.checks_per_day


def resolve_airport(text: str) -> tuple[str, ...]:
    """The airports a name could mean. More than one means the person has to choose."""
    name = " ".join(re.sub(r"[.,]", " ", text).lower().split())
    name = _CITY_NICKNAMES.get(name, name)
    if name in _CITY_AIRPORTS:
        return _CITY_AIRPORTS[name]
    if re.fullmatch(r"[a-z]{3}", name):
        return (name.upper(),)
    return ()


def _month(name: str) -> int:
    if name not in _MONTHS:
        raise ValueError(f"I don't recognize the month {name!r}")
    return _MONTHS[name]


def _on(year: int, month: int, day: int) -> date:
    try:
        return date(year, month, day)
    except ValueError as exc:
        raise ValueError(f"{calendar.month_name[month]} {day}, {year} isn't a real date") from exc


def _upcoming(month: int, day: int, today: date) -> date:
    # Feb 29 only exists in some years, so look ahead until it fits.
    for year in range(today.year, today.year + 9):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= today:
            return candidate
    raise ValueError(f"{calendar.month_name[month]} {day} isn't a real date")


def parse_dates(text: str, today: date) -> str:
    """Turn 'May 21-23 2027' and similar into the form ``service.parse_dates`` accepts.

    A date with no year means its next occurrence, which the review shows before saving.
    Numeric dates like 5/6/2027 are refused, because they read differently around the world.
    """
    cleaned = text.strip().lower()
    if not cleaned:
        raise ValueError("no dates given")
    if re.match(r"^\d{4}-", cleaned):
        service.parse_dates(cleaned)
        return cleaned
    if "/" in cleaned:
        raise ValueError("please write the month as a word, like May 21 2027")
    cleaned = re.sub(r"(\d)(?:st|nd|rd|th)\b", r"\1", cleaned)
    cleaned = re.sub(r",\s*(\d{4})\b", r" \1", cleaned)

    matches = []
    for part in re.split(r"\s*(?:,|;|&|\band\b)\s*", cleaned):
        if part:
            match = _DATE_PART.match(part)
            if match is None:
                raise ValueError("I couldn't read those dates. Try something like May 21-23 2027")
            matches.append(match)
    if not matches:
        raise ValueError("no dates given")

    years = [int(y) for m in matches for y in (m["y1"], m["y2"]) if y]
    stated_year = years[-1] if years else None
    month: int | None = None
    pieces = []
    for match in matches:
        if match["m1"]:
            month = _month(match["m1"])
        if month is None:
            raise ValueError("please include the month, like May 21 2027")
        start_month = month
        day = int(match["d1"])
        end_month = _month(match["m2"]) if match["m2"] else month
        month = end_month

        if match["d2"] is None:
            year = int(match["y1"]) if match["y1"] else stated_year
            start = _on(year, start_month, day) if year else _upcoming(start_month, day, today)
            pieces.append(start.isoformat())
            continue

        end_day = int(match["d2"])
        start_year = match["y1"] or match["y2"] or stated_year
        if start_year:
            start = _on(int(start_year), start_month, day)
            end_year = int(match["y2"] or start_year)
            end = _on(end_year, end_month, end_day)
            if end < start and not match["y1"]:  # Dec 30 - Jan 2 2027 starts the year before
                start = _on(end_year - 1, start_month, day)
        else:
            start = _upcoming(start_month, day, today)
            end = _on(start.year, end_month, end_day)
            if end < start:
                end = _on(start.year + 1, end_month, end_day)
        pieces.append(f"{start.isoformat()}..{end.isoformat()}")

    result = ",".join(pieces)
    service.parse_dates(result)
    return result


def parse_cabin(text: str) -> Cabin:
    name = " ".join(text.lower().split()).removesuffix(" class").strip()
    if name not in _CABINS:
        raise ValueError("please choose economy, premium, business or first")
    return _CABINS[name]


def parse_pax(text: str) -> int:
    stripped = text.strip()
    if not stripped.isdigit() or not 1 <= int(stripped) <= 9:
        raise ValueError("please enter a number of passengers from 1 to 9")
    return int(stripped)


def parse_miles(text: str) -> int | None:
    """'120k' or '120,000' miles. Blank or 'no limit' means no limit."""
    cleaned = text.strip().lower().replace(",", "")
    if cleaned in ("", "none", "no", "no limit", "any"):
        return None
    match = _MILES.match(cleaned)
    if match is None:
        raise ValueError("please enter a number of miles like 120k, or press Enter for no limit")
    miles = float(match[1]) * (1000 if match[2] else 1)
    if miles <= 0 or miles != int(miles):
        raise ValueError("please enter a whole number of miles above zero")
    return int(miles)


def _format_day(day: date) -> str:
    return day.strftime("%a, %b %d, %Y")


def review_text(draft: WatchDraft, owner: str) -> str:
    """What's about to be saved, with what it costs in searches."""
    days = service.parse_dates(draft.dates or "")
    limit = f"{draft.max_miles:,} miles per person" if draft.max_miles else "no limit"
    addresses = service.parse_recipients(draft.recipients)
    searches = draft.searches_per_day
    site = get_site(draft.program)
    lines = [
        f"Flight:      {draft.flight} from {draft.origin} to {draft.destination}",
        f"Cabin:       {draft.cabin.value}",
        f"Miles:       {site.label}",
        f"Passengers:  {draft.pax}",
        f"Dates:       {_format_day(days[0])}",
        *(f"             {_format_day(day)}" for day in days[1:]),
        f"Price limit: {limit}",
        f"Alerts go to: {', '.join(addresses) if addresses else owner}",
        f"Searching:   about {searches} {site.domain} searches a day",
    ]
    if searches > HIGH_VOLUME_SEARCHES:
        lines.append(
            f"             That's a lot, and more searching makes a {site.domain} block more "
            "likely. Fewer dates would help."
        )
    return "\n".join(lines)


def _fields(draft: WatchDraft) -> dict[str, Any]:
    missing = draft.missing()
    if missing:
        raise ValueError(f"still needed: {', '.join(missing)}")
    return {
        "flight": draft.flight or "",
        "origin": draft.origin or "",
        "destination": draft.destination or "",
        "dates": draft.dates or "",
        "cabin": draft.cabin.value,
        "program": draft.program,
        "pax": draft.pax,
        "checks_per_day": draft.checks_per_day,
        "max_miles": draft.max_miles,
        "recipients": draft.recipients,
    }


def validate(draft: WatchDraft) -> None:
    """Raise ``ValueError`` if saving this draft would be refused, so a review can't mislead."""
    service.validate_watch(**_fields(draft))


def save(store: Store, draft: WatchDraft) -> Watch:
    """Save a finished draft through the one validated way of creating a watch."""
    return service.create_watch(store, **_fields(draft))


def first_upcoming_day(dates: str, today: date) -> date:
    """The day to search when listing flights. Raises if every date has passed."""
    upcoming = [day for day in service.parse_dates(dates) if day >= today]
    if not upcoming:
        raise ValueError("all of those dates have passed")
    return upcoming[0]
