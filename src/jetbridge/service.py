"""Wiring shared by the CLI and the MCP server."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from jetbridge import headlines
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.sites import PROGRAMS, get_site
from jetbridge.matcher import evaluate, is_single_flight
from jetbridge.models import (
    AnswerKind,
    Cabin,
    CheckResult,
    CheckStatus,
    Flight,
    Query,
    SearchAnswer,
    Watch,
)
from jetbridge.paths import db_path, lock_path
from jetbridge.store import NewAlert, Store

_AIRPORT = re.compile(r"^[A-Z]{3}$")
_EMAIL = re.compile(r"[^@ ,;]+@[^@ ,;.]+(\.[^@ ,;.]+)+")
MAX_DATES = 14
MAX_RECIPIENTS = 10
FIND_FLIGHTS_BUDGET_S = 180


class TickBusy(Exception):
    """Another jetbridge check is running, and only one may use the Chrome profile at a time."""


@contextmanager
def tick_lock() -> Iterator[None]:
    """An OS file lock, released automatically even if the process crashes."""
    import msvcrt

    handle = open(lock_path(), "a+b")  # noqa: SIM115 (held for the whole block)
    try:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise TickBusy("another jetbridge check is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        handle.close()


RISK_ACCEPTED = "risk_accepted"
RISK_NOTICE = """Before you start, please read this:

- jetbridge searches airline websites (aa.com, delta.com, united.com) automatically, which likely
  goes against their terms of use. You accept that risk by running it. It never logs in, so your
  frequent flyer accounts aren't involved, and it searches at low volume, but a site can still
  block its searches.
- It only checks while this PC is on, awake and logged in. To catch seats overnight, keep it awake
  while plugged in. jetbridge can set that for you if you ask.
- Checks are hours apart, so seats that open and close between them are missed. Don't rely on it
  as your only way of finding seats.
- It never books anything or moves your miles. You do that yourself.
- Only add email addresses of people who want these alerts.
"""


def open_store() -> Store:
    return Store(db_path())


def parse_dates(text: str) -> tuple[date, ...]:
    """Accepts ``2027-06-14..2027-06-16`` and comma-separated dates or ranges."""
    days: set[date] = set()
    for part in text.split(","):
        part = part.strip()
        if ".." in part:
            start_text, end_text = part.split("..", 1)
            start, end = date.fromisoformat(start_text), date.fromisoformat(end_text)
            if end < start:
                raise ValueError(f"range ends before it starts: {part}")
            if (end - start).days >= MAX_DATES:
                raise ValueError(f"at most {MAX_DATES} dates per watch")
            days.update(start + timedelta(days=i) for i in range((end - start).days + 1))
        elif part:
            days.add(date.fromisoformat(part))
    if not days:
        raise ValueError("no dates given")
    if len(days) > MAX_DATES:
        raise ValueError(f"at most {MAX_DATES} dates per watch")
    return tuple(sorted(days))


def parse_recipients(text: str | None) -> tuple[str, ...]:
    """Comma, semicolon or space separated addresses. Empty or 'me' means the owner."""
    parts = (text or "").replace(";", " ").replace(",", " ").split()
    if [p.lower() for p in parts] == ["me"]:
        return ()
    found: list[str] = []
    for part in parts:
        address = part.lower()
        if not _EMAIL.fullmatch(address):
            raise ValueError(f"not an email address: {part!r}")
        if address not in found:
            found.append(address)
    if len(found) > MAX_RECIPIENTS:
        raise ValueError(f"at most {MAX_RECIPIENTS} recipients per watch")
    return tuple(found)


@dataclass(frozen=True)
class ValidWatch:
    """The fields of a watch that passed validation, in the form the store takes."""

    flight: Flight
    origin: str
    destination: str
    days: tuple[date, ...]
    cabin: Cabin
    recipients: tuple[str, ...]
    now: float


def validate_watch(
    *,
    flight: str,
    origin: str,
    destination: str,
    dates: str,
    cabin: str,
    program: str = "american",
    pax: int = 1,
    checks_per_day: int = 6,
    max_miles: int | None = None,
    recipients: str | None = None,
    now: float | None = None,
) -> ValidWatch:
    """Raise ``ValueError`` unless these fields make a watch. Saves nothing."""
    parsed = Flight.parse(flight)
    origin, destination = origin.strip().upper(), destination.strip().upper()
    for airport in (origin, destination):
        if not _AIRPORT.match(airport):
            raise ValueError(f"not an airport code: {airport!r}")
    if origin == destination:
        raise ValueError("origin and destination are the same")
    if program not in PROGRAMS:
        raise ValueError(f"unsupported program {program!r}; available: {', '.join(PROGRAMS)}")
    if not 1 <= pax <= 9:
        raise ValueError("pax must be 1-9")
    if not 1 <= checks_per_day <= 24:
        raise ValueError("checks per day must be 1-24")
    if max_miles is not None and max_miles <= 0:
        raise ValueError("max miles must be positive")
    days = parse_dates(dates)
    addresses = parse_recipients(recipients)
    current = now if now is not None else time.time()
    today = datetime.fromtimestamp(current).date()
    if all(d < today for d in days):
        raise ValueError("all of those dates have passed")
    return ValidWatch(
        parsed, origin, destination, days, Cabin(cabin.strip().lower()), addresses, current
    )


def create_watch(
    store: Store,
    *,
    flight: str,
    origin: str,
    destination: str,
    dates: str,
    cabin: str,
    program: str = "american",
    pax: int = 1,
    checks_per_day: int = 6,
    max_miles: int | None = None,
    recipients: str | None = None,
    now: float | None = None,
) -> Watch:
    """Validate and create a watch. The one way a watch gets saved."""
    valid = validate_watch(
        flight=flight,
        origin=origin,
        destination=destination,
        dates=dates,
        cabin=cabin,
        program=program,
        pax=pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
        recipients=recipients,
        now=now,
    )
    watch = store.add_watch(
        valid.flight,
        valid.origin,
        valid.destination,
        valid.days,
        valid.cabin,
        now=valid.now,
        program=program,
        pax=pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
        recipients=valid.recipients,
    )
    welcome_new_recipients(store, watch, _owner_address(), valid.now)
    return watch


def change_watch(
    store: Store,
    watch: Watch,
    *,
    dates: str,
    checks_per_day: int,
    max_miles: int | None,
    now: float | None = None,
) -> Watch:
    """Change what can change without making a watch's history wrong. New dates are checked on the
    next run, and a finished watch given new dates starts again."""
    valid = validate_watch(
        flight=watch.flight.code,
        origin=watch.origin,
        destination=watch.destination,
        dates=dates,
        cabin=watch.cabin.value,
        program=watch.program,
        pax=watch.pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
        recipients=",".join(watch.recipients),
        now=now,
    )
    store.update_watch(watch.id, valid.days, checks_per_day, max_miles)
    if set(valid.days) - set(watch.dates):
        store.set_next_check(watch.id, valid.now)
    if watch.status == "finished":
        store.set_watch_status(watch.id, "active")
    updated = store.get_watch(watch.id)
    assert updated is not None
    return updated


def first_check_means(watch: Watch, result: CheckResult) -> str:
    """What a result says about whether this watch was set up correctly, in plain words."""
    domain = get_site(watch.program).domain
    if result.status is CheckStatus.AVAILABLE:
        price = f", at {result.miles:,} miles" if result.miles else ""
        return f"Seats are open now{price}."
    if result.status is CheckStatus.NOT_OFFERED and result.miles:
        return f"{domain} lists this flight, with seats above your price limit: {result.detail}."
    if result.status is CheckStatus.NOT_OFFERED:
        return f"{domain} lists this flight, but has no qualifying seats yet. That's working."
    if result.status is CheckStatus.NOT_LISTED:
        return (
            f"{domain} answered, but this flight wasn't in its results. Check the flight number "
            f"and airports on a flight tracker. {domain} may also not sell this flight with these "
            "miles, or may hide it while it has no award seats."
        )
    return f"jetbridge couldn't tell this time ({result.detail}). It'll keep trying."


def welcome_body(watch: Watch) -> str:
    days = ", ".join(f"{day:%b %d, %Y}" for day in watch.dates)
    site = get_site(watch.program)
    return (
        "The person sending this email added you to their jetbridge flight watch, so you'll get "
        "an email if award seats open on:\n\n"
        f"  {watch.flight.code} {watch.origin} to {watch.destination}, {watch.cabin.value}\n"
        f"  {days}\n"
        f"  Using {site.label} miles\n\n"
        "You don't need to do or install anything. So you don't miss an alert, please mark this "
        'email as "not spam" if it landed there, and add the sender to your contacts.'
    )


def welcome_new_recipients(store: Store, watch: Watch, owner: str | None, now: float) -> list[str]:
    """Queue one note for each person new to jetbridge's alerts, and return who they are.

    A person only ever gets one, however many watches they're added to. It also shows up a bad
    address before a real alert goes missing.
    """
    already = {
        row["recipient"]
        for row in store.conn.execute("SELECT recipient FROM outbox WHERE kind='welcome'")
    }
    new = [a for a in watch.recipients if a != owner and a not in already]
    for address in new:
        store.enqueue(
            NewAlert(
                "welcome",
                f"You've been added to a flight alert for {watch.flight.code}",
                welcome_body(watch),
                dedupe_key=f"welcome:{address}",
                recipient=address,
            ),
            now,
        )
    return new


def _owner_address() -> str | None:
    from jetbridge.notify.email import load_config

    try:
        config = load_config()
    except ValueError:
        return None
    return config.to_address if config else None


def set_recipients(
    store: Store, watch: Watch, recipients: tuple[str, ...]
) -> tuple[Watch, list[str]]:
    """Change who a watch alerts. Returns the watch and who'll get a welcome note."""
    store.set_recipients(watch.id, recipients)
    updated = store.get_watch(watch.id)
    assert updated is not None
    return updated, welcome_new_recipients(store, updated, _owner_address(), time.time())


@dataclass(frozen=True)
class FlightOption:
    """One flight a site listed for a route and date, as a watch would see it."""

    code: str
    departs: datetime
    arrives: datetime
    stops: int
    result: CheckResult


@dataclass(frozen=True)
class FlightSearch:
    """A route search. ``problem`` means no answer was given, not that there's no flight."""

    options: tuple[FlightOption, ...] = ()
    problem: str | None = None
    incomplete: bool = False


_NO_ANSWER = {
    AnswerKind.BLOCKED: "{site} asked jetbridge to slow down, so it won't try again for a while.",
    AnswerKind.DEFERRED: "jetbridge is holding off on searches right now to stay under its limit.",
    AnswerKind.DRIFT: "{site}'s page wasn't what jetbridge expected, so it can't list flights.",
    AnswerKind.ERROR: "the search didn't finish, so jetbridge can't list flights right now.",
}


def flights_from_answer(answer: SearchAnswer, query: Query, cabin: Cabin) -> FlightSearch:
    """Read the flights out of an answer. Each is judged by the same rules a watch uses."""
    domain = get_site(query.program).domain
    if answer.kind is not AnswerKind.ANSWER or answer.parsed is None:
        problem = _NO_ANSWER.get(answer.kind, "{site} gave no clear answer.")
        return FlightSearch(problem=problem.format(site=domain))
    parsed = answer.parsed
    if parsed.site_error:
        return FlightSearch(problem=f"{domain} returned an error ({parsed.site_error}).")

    options: dict[str, FlightOption] = {}
    for item in parsed.slices:
        if not is_single_flight(item, query):
            continue
        first, last = item.legs[0], item.legs[-1]
        watch = Watch(
            id=0,
            flight=Flight(first.carrier, first.number),
            origin=query.origin,
            destination=query.destination,
            dates=(query.date,),
            cabin=cabin,
            program=query.program,
            pax=query.pax,
        )
        options.setdefault(
            first.code,
            FlightOption(
                first.code,
                first.departs_at,
                last.arrives_at,
                len(item.legs) - 1,
                evaluate(watch, query.date, answer),
            ),
        )
    ordered = sorted(options.values(), key=lambda option: option.departs)
    return FlightSearch(tuple(ordered), incomplete=bool(parsed.unreadable))


async def search_flights(fetcher, query: Query, cabin: Cabin, deadline: float) -> FlightSearch:
    """One search and no retries. A block has already opened a rest in the governor."""
    try:
        answer = await fetcher.search(query, deadline)
    finally:
        await fetcher.close()
    return flights_from_answer(answer, query, cabin)


def find_flights(store: Store, query: Query, cabin: Cabin) -> FlightSearch:
    """List the flights on a route, using one real search under the usual lock and limits."""
    from jetbridge.acquire.fetcher import LiveFetcher

    with tick_lock():
        governor = Governor(store, time.time)
        deadline = time.time() + FIND_FLIGHTS_BUDGET_S
        return asyncio.run(search_flights(LiveFetcher(governor), query, cabin, deadline))


def live_tick(store: Store, only_watch: int | None = None):
    """Run one real tick under the lock. Imports the browser layer only when it's needed."""
    from jetbridge.acquire.fetcher import LiveFetcher
    from jetbridge.notify.email import GmailSender
    from jetbridge.runner import Runner

    with tick_lock():
        governor = Governor(store, time.time)
        runner = Runner(
            store,
            LiveFetcher(governor),
            GmailSender(),
            governor,
            clock=time.time,
            headlines=headlines.load(),
        )
        return asyncio.run(runner.tick(only_watch=only_watch))
