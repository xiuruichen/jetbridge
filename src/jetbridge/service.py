"""Wiring shared by the CLI and the MCP server."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from jetbridge.acquire.governor import Governor
from jetbridge.models import Cabin, Flight, Watch
from jetbridge.paths import db_path, lock_path
from jetbridge.store import Store

_AIRPORT = re.compile(r"^[A-Z]{3}$")
MAX_DATES = 14
PROGRAMS = ("american",)


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


def open_store() -> Store:
    return Store(db_path())


def parse_dates(text: str) -> tuple[date, ...]:
    """Accepts ``2027-05-21..2027-05-23`` and comma-separated dates or ranges."""
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
    now: float | None = None,
) -> Watch:
    """Validate and create a watch. The one entry point for the CLI and MCP server."""
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
    current = now if now is not None else time.time()
    today = datetime.fromtimestamp(current).date()
    if all(d < today for d in days):
        raise ValueError("all of those dates have passed")
    return store.add_watch(
        parsed,
        origin,
        destination,
        days,
        Cabin(cabin.strip().lower()),
        now=current,
        program=program,
        pax=pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
    )


def live_tick(store: Store, only_watch: int | None = None):
    """Run one real tick under the lock. Imports the browser layer only when it's needed."""
    from jetbridge.acquire.fetcher import LiveFetcher
    from jetbridge.notify.email import GmailSender
    from jetbridge.runner import Runner

    with tick_lock():
        governor = Governor(store, time.time)
        runner = Runner(store, LiveFetcher(governor), GmailSender(), governor, clock=time.time)
        return asyncio.run(runner.tick(only_watch=only_watch))
