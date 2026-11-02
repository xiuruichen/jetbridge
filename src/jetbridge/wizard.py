"""The questions behind `jetbridge start`. Nothing is saved until the person says yes."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date

import typer

from jetbridge import draft as drafts
from jetbridge import service
from jetbridge.acquire.sites import get_site, parse_program
from jetbridge.draft import WatchDraft
from jetbridge.models import Cabin, CheckStatus, Flight, Query, Watch
from jetbridge.service import FlightOption, FlightSearch, TickBusy
from jetbridge.store import Store


def _ask[T](label: str, parse: Callable[[str], T], default: str | None = None) -> T:
    """Keep asking until the answer makes sense."""
    while True:
        text = typer.prompt(label, default=default, show_default=bool(default))
        try:
            return parse(text)
        except ValueError as exc:
            typer.echo(f"  {exc}")


def _pick_airport(label: str) -> str:
    while True:
        text = typer.prompt(f"{label} (city or airport code)")
        airports = drafts.resolve_airport(text)
        if len(airports) == 1:
            return airports[0]
        if not airports:
            typer.echo(
                f"  I don't know {text!r}. Try the 3-letter airport code, which a quick "
                "web search will find."
            )
            continue
        typer.echo(f"  {text.strip().title()} has more than one airport:")
        for number, airport in enumerate(airports, 1):
            typer.echo(f"    {number}. {airport}")
        choice = typer.prompt("  Which one? (number or code)").strip().upper()
        if choice.isdigit() and 1 <= int(choice) <= len(airports):
            return airports[int(choice) - 1]
        if len(choice) == 3 and choice.isalpha():
            return choice
        typer.echo("  Let's try that again.")


def _ask_route() -> tuple[str, str]:
    while True:
        origin = _pick_airport("Flying from")
        destination = _pick_airport("Flying to")
        if origin != destination:
            return origin, destination
        typer.echo("  Those are the same airport. Let's try again.")


def _ask_dates(today: date) -> str:
    def parse(text: str) -> str:
        dates = drafts.parse_dates(text, today)
        drafts.first_upcoming_day(dates, today)
        return dates

    return _ask("Which dates? (like May 21-23 2027)", parse)


def _plain_seats(option: FlightOption, cabin: Cabin) -> str:
    result = option.result
    if result.status is CheckStatus.AVAILABLE:
        price = f": {result.miles:,} miles" if result.miles else ""
        return f"{cabin.value} seats open right now{price}"
    if result.status is CheckStatus.NOT_OFFERED:
        return f"no {cabin.value} seats right now"
    return "couldn't tell if seats are open"


def _show_options(options: list[FlightOption], cabin: Cabin) -> None:
    for number, option in enumerate(options, 1):
        stops = "" if option.stops == 0 else f", {option.stops} stop(s)"
        typer.echo(
            f"  {number}. {option.code}, departs {option.departs.strftime('%I:%M %p')}, "
            f"arrives {option.arrives.strftime('%I:%M %p %b %d')}{stops}"
        )
        typer.echo(f"     {_plain_seats(option, cabin)}")


def _search(query: Query, cabin: Cabin) -> FlightSearch | None:
    domain = get_site(query.program).domain
    typer.echo(f"Searching {domain} for flights on {query.date:%b %d, %Y}. This takes a minute...")
    try:
        return service.find_flights(service.open_store(), query, cabin)
    except TickBusy:
        typer.echo("  A scheduled check is using Chrome right now, so I can't search.")
        return None


def _ask_typed_flight() -> str | None:
    while True:
        text = typer.prompt(
            "Flight number (like AA100, or Enter to stop)", default="", show_default=False
        ).strip()
        if not text:
            return None
        try:
            return Flight.parse(text).code
        except ValueError as exc:
            typer.echo(f"  {exc}")


def _choose_flight(query: Query, cabin: Cabin) -> str | None:
    """A flight number the person typed or picked. None means they stopped."""
    text = typer.prompt(
        "Flight number if you know it, or press Enter and I'll look up the flights",
        default="",
        show_default=False,
    ).strip()
    if text:
        try:
            return Flight.parse(text).code
        except ValueError as exc:
            typer.echo(f"  {exc}")
            return _ask_typed_flight()

    domain = get_site(query.program).domain
    search = _search(query, cabin)
    if search is None:
        return _ask_typed_flight()
    if search.problem:
        # Never retried here: a block has to rest, and a search can be run again later.
        typer.echo(f"  {search.problem}")
        typer.echo("  If you know the flight number you can still add it.")
        return _ask_typed_flight()
    if not search.options:
        typer.echo(
            f"  {domain} didn't list a single flight for that route and date. It can leave out "
            "flights with no award seats, so the flight may still exist. A flight tracker "
            "will show its number."
        )
        return _ask_typed_flight()

    _show_options(search.options, cabin)
    typer.echo("  Connections aren't shown, because jetbridge watches one flight at a time.")
    if search.incomplete:
        typer.echo(f"  Part of {domain}'s answer couldn't be read, so this list may be incomplete.")
    typer.echo(f"  {domain} can leave out flights with no award seats, so yours may not be listed.")
    while True:
        choice = typer.prompt(
            "Which one? (number, a flight number, or Enter to stop)",
            default="",
            show_default=False,
        ).strip()
        if not choice:
            return None
        if choice.isdigit() and 1 <= int(choice) <= len(search.options):
            return search.options[int(choice) - 1].code
        try:
            if choice.isdigit():
                raise ValueError(f"please choose a number from 1 to {len(search.options)}")
            return Flight.parse(choice).code
        except ValueError as exc:
            typer.echo(f"  {exc}")


def _ask_recipients(owner: str) -> str:
    def parse(text: str) -> str:
        service.parse_recipients(text)
        return text

    return _ask(
        f"Who should get seat alerts? (emails separated by commas, or Enter for {owner})",
        parse,
        default="",
    )


def add_one(store: Store, owner: str, today: date) -> Watch | None:
    origin, destination = _ask_route()
    dates = _ask_dates(today)
    cabin = _ask(
        "Which cabin? (economy, premium, business or first)", drafts.parse_cabin, "business"
    )
    pax = _ask("How many passengers?", drafts.parse_pax, "1")
    program = _ask("Which miles? (american, delta or united)", parse_program, "american")

    day = drafts.first_upcoming_day(dates, today)
    flight = _choose_flight(Query(origin, destination, day, pax, program), cabin)
    if flight is None:
        typer.echo("OK, nothing was saved.")
        return None

    max_miles = _ask(
        "Most miles you'd pay per person? (like 120k, or Enter for no limit)",
        drafts.parse_miles,
        default="",
    )
    recipients = _ask_recipients(owner)

    draft = WatchDraft(
        origin=origin,
        destination=destination,
        dates=dates,
        flight=flight,
        cabin=cabin,
        program=program,
        pax=pax,
        max_miles=max_miles,
        recipients=recipients,
    )
    typer.echo("\nHere's what I'll watch:")
    typer.echo(drafts.review_text(draft, owner))
    if not typer.confirm("\nStart watching this flight?", default=False):
        typer.echo("OK, nothing was saved.")
        return None
    try:
        watch = drafts.save(store, draft)
    except ValueError as exc:
        typer.echo(f"I couldn't save that: {exc}. Nothing was saved.")
        return None
    typer.echo(
        f"Saved as watch {watch.id}. The first check runs within about 15 minutes "
        "once checks are turned on."
    )
    return watch


def add_watches(store: Store, owner: str, today: date) -> list[Watch]:
    added: list[Watch] = []
    while True:
        watch = add_one(store, owner, today)
        if watch:
            added.append(watch)
        if not typer.confirm("\nAdd another flight?", default=False):
            return added
        typer.echo("")
