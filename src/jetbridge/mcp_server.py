"""Optional Claude layer: a thin wrapper over the same functions the CLI uses.

Checks and alerts never depend on this. All results come from the deterministic engine.
"""

from __future__ import annotations

import functools
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Hashable
from concurrent import futures
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from jetbridge import doctor, headlines, service, winsched, wizard
from jetbridge import draft as drafts
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.sites import SITES, parse_program
from jetbridge.alerts import DEFAULT_HEADLINES, AlertKind, format_when
from jetbridge.models import Cabin, Flight, Query, Watch
from jetbridge.notify import email as mail

server = MCPServer(
    "jetbridge",
    instructions=(
        "jetbridge watches award seats on one specific flight and emails when they open. "
        "Everything it can do is available through these tools, so the person never needs a "
        "command. Talk plainly: don't mention tool names, review ids, check ids, watch numbers "
        "or commands, and work out which watch they mean from list_watches. If two "
        "could match, ask which.\n\n"
        "First time, or if they ask for help getting started: call get_started and follow its "
        "next_step. Show its notice word for word and call accept_notice only after they agree.\n\n"
        "Adding a watch needs the flight, its departure and arrival airports, dates, cabin, miles "
        "program, party size and who gets the alerts. Ask for anything missing and never guess. "
        "City names are fine; if a city has several airports, ask which. If they don't know the "
        "flight number, offer find_flights, then let them choose. Call review_watch, show the "
        "review word for word, and call confirm_watch only after they say yes to it. If they want "
        "a change, call review_watch again.\n\n"
        "Changing a watch's flight, dates, cabin, program, party size, price limit or checks per "
        "day: review and confirm the new version, then offer to remove the old one. Only "
        "recipients can be changed in place.\n\n"
        "Removing a watch takes two steps too: review_watch_removal, show what it says, and "
        "confirm_watch_removal only after a yes. Before turning off automatic checks, say that no "
        "watch will be checked until they're turned back on, and wait for a yes. Read addresses "
        "back before adding someone to alerts or sending them a test email.\n\n"
        "A live check or flight lookup can take a few minutes: if an answer says finished false, "
        "tell them it's still going and call check_result with its check_id until it finishes. A "
        "check can email everyone on that watch if seats have opened, so say so if they're "
        "checking a watch that alerts other people.\n\n"
        "The Gmail app password must never go in this chat. To set up email, use open_email_setup, "
        "which opens a private window on this PC. If someone pastes a password here, tell them to "
        "revoke it in their Google account and make a new one.\n\n"
        "Quote only numbers returned by these tools. 'unknown' means the check couldn't tell, so "
        "never describe it as no seats. A flight missing from a lookup may still exist. jetbridge "
        "can't book anything or move miles."
    ),
)


def _tool[**P, R](*, read_only: bool = False) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a tool whose refusals reach Claude, which a plain exception's message wouldn't."""

    def register(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return function(*args, **kwargs)
            except (ValueError, service.TickBusy, mail.NotConfigured) as exc:
                raise ToolError(str(exc)) from exc

        server.tool(annotations=ToolAnnotations(readOnlyHint=read_only))(guarded)
        return function

    return register


def _watch_view(watch: Watch) -> dict[str, Any]:
    store = service.open_store()
    days = []
    for day in watch.dates:
        state = store.date_state(watch.id, day)
        days.append(
            {
                "date": day.isoformat(),
                "status": state.status.value if state.status else "no clear answer yet",
                "miles": state.miles,
                "last_clear_answer": format_when(state.last_conclusive_at)
                if state.last_conclusive_at
                else None,
            }
        )
    return {
        "id": watch.id,
        "flight": watch.flight.code,
        "route": f"{watch.origin}-{watch.destination}",
        "cabin": watch.cabin.value,
        "program": watch.program,
        "pax": watch.pax,
        "checks_per_day": watch.checks_per_day,
        "max_miles": watch.max_miles,
        "seat_alerts_to": list(watch.recipients) or ["the owner"],
        "status": watch.status,
        "next_check": format_when(watch.next_check_at),
        "dates": days,
    }


def _require_watch(watch_id: int) -> Watch:
    watch = service.open_store().get_watch(watch_id)
    if watch is None:
        raise ToolError(f"no watch with id {watch_id}")
    return watch


def _require_notice() -> None:
    """The same rule as `jetbridge start`: nothing is searched until the notice is accepted."""
    if service.open_store().get(service.RISK_ACCEPTED, "0") != "1":
        raise ToolError(
            "the person hasn't accepted jetbridge's notice yet. Call get_started and show it first"
        )


def _airport(text: str) -> str:
    airports = drafts.resolve_airport(text)
    if len(airports) == 1:
        return airports[0]
    if airports:
        raise ToolError(
            f"{text.strip()} has more than one airport ({', '.join(airports)}). Ask which one "
            "this flight uses"
        )
    raise ToolError(f"I don't know the airport {text.strip()!r}. Use its 3-letter code")


def _owner_address() -> str | None:
    config = mail.load_config()
    return config.to_address if config else None


def _email_ready() -> bool:
    config = mail.load_config()
    return bool(config and mail.keyring.get_password(mail.KEYRING_SERVICE, config.from_address))


# ---- getting started ------------------------------------------------------------------
@_tool(read_only=True)
def get_started() -> dict[str, Any]:
    """What's set up and what to do next. Use this first, or when someone asks for help."""
    store = service.open_store()
    accepted = store.get(service.RISK_ACCEPTED, "0") == "1"
    email = _email_ready()
    checks_on = winsched.is_installed()
    watches = len(store.list_watches())
    if not accepted:
        next_step = "Show the notice word for word, then call accept_notice if they agree."
    elif not email:
        next_step = (
            "Set up alert emails. They'll need a Gmail app password from "
            "https://myaccount.google.com/apppasswords, which needs 2-Step Verification. Call "
            "open_email_setup when they have it."
        )
    elif not watches:
        next_step = "Ask which flight they want to watch."
    elif not checks_on:
        next_step = "Offer to turn on automatic checks with set_automatic_checks."
    else:
        next_step = "Everything is set up. Ask what they'd like to do."
    answer: dict[str, Any] = {
        "notice_accepted": accepted,
        "email_set_up": email,
        "alerts_go_to": _owner_address(),
        "automatic_checks_on": checks_on,
        "watches": watches,
        "next_step": next_step,
    }
    if not accepted:
        answer["notice"] = service.RISK_NOTICE
    return answer


@_tool()
def accept_notice() -> dict[str, Any]:
    """Record that the person read jetbridge's notice and agreed. Call only after their yes."""
    service.open_store().set(service.RISK_ACCEPTED, "1")
    return {"notice_accepted": True}


@_tool()
def open_email_setup() -> dict[str, str]:
    """Open a private window on this PC for the Gmail address and app password.

    The password is typed there, hidden, and goes straight to Windows Credential Manager. Never
    ask for it in the chat. The window sends a test email when it's done."""
    subprocess.Popen(
        [sys.executable, "-m", "jetbridge", "notify", "setup-window"],
        creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    return {
        "message": "A window opened on this PC, possibly behind this one. It asks for the Gmail "
        "address to send from, the address alerts go to, and the app password, then sends a test "
        "email. Ask them to say when it's done, then call get_started to confirm."
    }


@_tool()
def send_test_email(to: str | None = None) -> dict[str, str]:
    """Send a test email to the owner, or to one address the person gave."""
    addresses = service.parse_recipients(to)
    if len(addresses) > 1:
        raise ToolError("test one address at a time")
    try:
        mail.GmailSender().send(
            "jetbridge test email",
            "If you can read this, jetbridge alerts will reach you.",
            addresses[0] if addresses else None,
        )
    except mail.NotConfigured:
        raise ToolError("email isn't set up yet. Call open_email_setup") from None
    except Exception as exc:
        raise ToolError(f"couldn't send: {exc}") from exc
    return {"sent_to": addresses[0] if addresses else (_owner_address() or "the owner")}


# ---- adding a watch -------------------------------------------------------------------
# Reviews waiting for a yes. Kept in memory, so one can't outlast this Claude session.
_reviews: dict[str, drafts.WatchDraft] = {}
_removals: dict[str, int] = {}


@_tool()
def review_watch(
    flight: str,
    origin: str,
    destination: str,
    dates: str,
    cabin: str,
    program: str,
    pax: int,
    checks_per_day: int = drafts.DEFAULT_CHECKS_PER_DAY,
    max_miles: int | None = None,
    notify: str | None = None,
) -> dict[str, Any]:
    """Check the details of a new watch and return a review. This saves nothing.

    Show the person the review word for word and ask them to confirm it. origin and destination
    are cities or airport codes. program is american, delta or united. dates is like May 21-23 2027
    or 2027-06-14..2027-06-16. notify is who gets seat alerts, comma-separated; leave it out to
    alert only the owner."""
    draft = drafts.WatchDraft(
        origin=_airport(origin),
        destination=_airport(destination),
        dates=drafts.parse_dates(dates, date.today()),
        flight=Flight.parse(flight).code,
        cabin=drafts.parse_cabin(cabin),
        program=parse_program(program),
        pax=pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
        recipients=notify,
    )
    drafts.validate(draft)
    review_id = secrets.token_hex(8)
    _reviews[review_id] = draft
    return {
        "saved": False,
        "review_id": review_id,
        "review": drafts.review_text(draft, _owner_address() or "you"),
    }


@_tool()
def confirm_watch(review_id: str) -> dict[str, Any]:
    """Save a reviewed watch. Call this only after the person has seen the review from
    review_watch and said yes to it. Each review can be saved once. Automatic checks are turned
    on if they aren't already."""
    _require_notice()
    draft = _reviews.pop(review_id, None)
    if draft is None:
        raise ToolError("no review with that id, so nothing was saved. Call review_watch first")
    try:
        watch = drafts.save(service.open_store(), draft)
    except ValueError as exc:
        raise ToolError(f"{exc}. Nothing was saved. Call review_watch again") from exc
    next_steps = []
    if not winsched.is_installed():
        try:
            winsched.install()
            next_steps.append("Automatic checks are now on, so the first check runs soon.")
        except RuntimeError as exc:
            next_steps.append(f"Automatic checks couldn't be turned on, so it won't run: {exc}")
    if not _email_ready():
        next_steps.append("Email isn't set up, so alerts will wait. Offer open_email_setup.")
    return {**_watch_view(watch), "next_steps": next_steps}


# ---- long searches --------------------------------------------------------------------
# Claude Desktop gives up on a tool call at about 60 seconds, and a live check can take longer
# because Chrome has to start and load the airline's site. So each one runs on its own thread and
# each call waits for less than that.
CHECK_WAIT_S = 45.0
_checks: dict[str, tuple[Hashable, futures.Future[dict[str, Any]]]] = {}


def _run_search(work: Callable[[], dict[str, Any]], future: futures.Future[dict[str, Any]]) -> None:
    try:
        future.set_result(work())
    except Exception as exc:
        future.set_exception(exc)


def _search_answer(check_id: str) -> dict[str, Any]:
    _key, future = _checks[check_id]
    if not futures.wait([future], timeout=CHECK_WAIT_S).done:
        return {
            "finished": False,
            "check_id": check_id,
            "message": "Still going. Call check_result with this check_id for the answer.",
        }
    del _checks[check_id]
    error = future.exception()
    if isinstance(error, service.TickBusy):
        raise ToolError(f"{error}. Try again in a few minutes")
    if error is not None:
        raise ToolError(f"that didn't finish: {error}")
    return {"finished": True, **future.result()}


def _start_search(key: Hashable, work: Callable[[], dict[str, Any]]) -> dict[str, Any]:
    """A search already running for the same thing is picked up rather than started again."""
    for check_id, (running, future) in _checks.items():
        if running == key and not future.done():
            return _search_answer(check_id)
    check_id = secrets.token_hex(8)
    future: futures.Future[dict[str, Any]] = futures.Future()
    _checks[check_id] = (key, future)
    threading.Thread(target=_run_search, args=(work, future), daemon=True).start()
    return _search_answer(check_id)


def _check_results(watch_id: int) -> dict[str, Any]:
    report = service.live_tick(service.open_store(), only_watch=watch_id)
    # An opening is checked twice before its alert goes out. The later answer is current.
    latest = {line.day: line for line in report.checks}
    return {
        "results": [
            {
                "date": line.day.isoformat(),
                "status": line.result.status.value,
                "miles": line.result.miles,
                "seats": line.result.seats_text,
                "detail": line.result.detail,
            }
            for line in latest.values()
        ],
        "emails_sent": report.alerts_sent,
        "stopped_early": report.stopped_early,
    }


@_tool()
def check_now(watch_id: int) -> dict[str, Any]:
    """Check a watch live now. Still subject to the rate limit and any rest after a block. If seats
    have opened, this emails everyone on the watch.

    If the answer says finished is false, the check is still running: call check_result with its
    check_id. stopped_early says why a check didn't happen, such as a site resting after a block."""
    _require_notice()
    watch = _require_watch(watch_id)
    if watch.status != "active":
        raise ToolError(f"that watch is {watch.status}. Resume it first")
    return _start_search(("check", watch_id), lambda: _check_results(watch_id))


@_tool(read_only=True)
def check_result(check_id: str) -> dict[str, Any]:
    """Get the answer to a check_now or find_flights that was still running. Waits up to 45 seconds,
    and says finished is false if it needs another call."""
    if check_id not in _checks:
        raise ToolError("nothing is running with that id. Start it again")
    return _search_answer(check_id)


def _flight_results(query: Query, cabin: Cabin) -> dict[str, Any]:
    search = service.find_flights(service.open_store(), query, cabin)
    domain = SITES[query.program].domain
    answer: dict[str, Any] = {
        "date": query.date.isoformat(),
        "route": f"{query.origin}-{query.destination}",
        "note": (
            f"Only single flights {domain} listed, with no connections. {domain} can leave out "
            "flights with no award seats, so one missing here may still exist."
        ),
    }
    if search.problem:
        return {**answer, "flights": [], "problem": search.problem}
    flights = [
        {
            "flight": option.code,
            "departs": option.departs.strftime("%I:%M %p"),
            "arrives": option.arrives.strftime("%I:%M %p %b %d"),
            "stops": option.stops,
            "seats": wizard.plain_seats(option, cabin),
        }
        for option in search.options
    ]
    return {**answer, "flights": flights, "list_may_be_incomplete": search.incomplete}


@_tool()
def find_flights(
    origin: str, destination: str, dates: str, cabin: str, program: str, pax: int
) -> dict[str, Any]:
    """List the flights on a route, for someone who doesn't know the flight number. Uses one real
    search of the first upcoming date. It can take a few minutes: if finished is false, call
    check_result with its check_id. Don't repeat a search that reported a problem."""
    _require_notice()
    if not 1 <= pax <= 9:
        raise ToolError("pax must be 1-9")
    start, end = _airport(origin), _airport(destination)
    if start == end:
        raise ToolError("origin and destination are the same")
    today = date.today()
    day = drafts.first_upcoming_day(drafts.parse_dates(dates, today), today)
    wanted = drafts.parse_cabin(cabin)
    query = Query(start, end, day, pax, parse_program(program))
    return _start_search(("find", query, wanted), lambda: _flight_results(query, wanted))


# ---- managing watches -----------------------------------------------------------------
@_tool(read_only=True)
def list_watches() -> list[dict[str, Any]]:
    """All watches with the current state of each date."""
    return [_watch_view(w) for w in service.open_store().list_watches()]


@_tool(read_only=True)
def watch_history(watch_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Recent check results for one watch, newest first."""
    _require_watch(watch_id)
    return [
        {
            "observed": format_when(row["observed_at"]),
            "date": row["day"],
            "status": row["status"],
            "miles": row["miles"],
            "seats": row["seats"],
            "detail": row["detail"],
        }
        for row in service.open_store().history(watch_id, limit)
    ]


@_tool()
def set_recipients(watch_id: int, notify: str) -> dict[str, Any]:
    """Replace who gets a watch's seat alerts. Comma-separated addresses, or 'me' for the owner."""
    _require_watch(watch_id)
    service.open_store().set_recipients(watch_id, service.parse_recipients(notify))
    return _watch_view(_require_watch(watch_id))


@_tool()
def pause_watch(watch_id: int) -> dict[str, Any]:
    """Stop checking a watch and keep its history. It can be resumed later."""
    _require_watch(watch_id)
    service.open_store().set_watch_status(watch_id, "paused")
    return _watch_view(_require_watch(watch_id))


@_tool()
def resume_watch(watch_id: int) -> dict[str, Any]:
    """Start checking a paused watch again, with a check on the next run."""
    _require_watch(watch_id)
    store = service.open_store()
    store.set_watch_status(watch_id, "active")
    store.set_next_check(watch_id, time.time())
    return _watch_view(_require_watch(watch_id))


@_tool()
def review_watch_removal(watch_id: int) -> dict[str, Any]:
    """Say what removing a watch would delete. This removes nothing. Show it and ask for a yes."""
    watch = _require_watch(watch_id)
    removal_id = secrets.token_hex(8)
    _removals[removal_id] = watch_id
    days = ", ".join(day.strftime("%b %d, %Y") for day in watch.dates)
    return {
        "removed": False,
        "removal_id": removal_id,
        "review": (
            f"Remove the watch on {watch.flight.code} from {watch.origin} to "
            f"{watch.destination}, {watch.cabin.value}, on {days}? This also deletes its history "
            "and can't be undone. Pausing it would keep the history."
        ),
    }


@_tool()
def confirm_watch_removal(removal_id: str) -> dict[str, Any]:
    """Delete a watch and its history. Call this only after the person said yes to the review from
    review_watch_removal. Each review can be used once."""
    watch_id = _removals.pop(removal_id, None)
    if watch_id is None:
        raise ToolError("no removal with that id, so nothing changed. Call review_watch_removal")
    watch = _require_watch(watch_id)
    service.open_store().delete_watch(watch_id)
    return {"removed": True, "flight": watch.flight.code}


# ---- settings -------------------------------------------------------------------------
_HEADLINE_MEANINGS = {
    AlertKind.SEATS_OPEN: "seats have opened",
    AlertKind.SEATS_OPEN_UNCONFIRMED: "seats appeared but the second check couldn't confirm them",
    AlertKind.PRICE_DROP: "open seats got cheaper",
    AlertKind.MORE_SEATS: "more seats have opened",
    AlertKind.REMINDER: "seats are still open",
    AlertKind.SEATS_GONE: "seats have gone",
}


@_tool(read_only=True)
def alert_settings() -> dict[str, Any]:
    """The daily summary, automatic checks, the owner's address and every alert headline."""
    custom = headlines.load()
    return {
        "daily_summary_on": service.open_store().get("digest_enabled", "1") == "1",
        "automatic_checks_on": winsched.is_installed(),
        "alerts_go_to": _owner_address(),
        "headlines": [
            {
                "kind": kind.value,
                "sent_when": _HEADLINE_MEANINGS[kind],
                "headline": custom.get(kind, DEFAULT_HEADLINES[kind]),
                "customized": kind in custom,
            }
            for kind in AlertKind
        ],
    }


@_tool()
def set_daily_summary(on: bool) -> dict[str, Any]:
    """Turn the daily summary email on or off. It also shows the watcher is still running."""
    service.open_store().set("digest_enabled", "1" if on else "0")
    return {"daily_summary_on": on}


@_tool()
def set_alert_headline(kind: str, text: str | None = None) -> dict[str, Any]:
    """Change the headline at the start of one kind of alert's subject, or leave text out to go
    back to the built-in one. kind comes from alert_settings. Only the wording changes, not when
    alerts are sent."""
    problem = headlines.problem()
    if problem:
        raise ToolError(f"this file needs fixing or deleting first: {problem}")
    alert = AlertKind(kind)
    custom = headlines.load()
    if text is None:
        custom.pop(alert, None)
    else:
        custom[alert] = headlines.clean(text)
    headlines.save(custom)
    return {"kind": alert.value, "headline": custom.get(alert, DEFAULT_HEADLINES[alert])}


@_tool()
def set_automatic_checks(on: bool) -> dict[str, Any]:
    """Turn automatic checks on or off. While they're off, no watch is checked and no alert is sent.
    Watches are kept. Before turning them off, tell the person that and wait for a yes."""
    if on:
        _require_notice()
    try:
        if on:
            winsched.install()
        elif winsched.is_installed():
            winsched.remove()
    except RuntimeError as exc:
        raise ToolError(str(exc)) from exc
    return {"automatic_checks_on": on}


# ---- health ---------------------------------------------------------------------------
@_tool(read_only=True)
def health() -> dict[str, Any]:
    """Whether the watcher is running: last tick, rate use, any rest period, emails queued."""
    store = service.open_store()
    governor = Governor(store, time.time)
    now = time.time()
    last = store.get("last_tick_at")
    return {
        "automatic_checks_on": winsched.is_installed(),
        "last_tick": format_when(float(last)) if last else None,
        "sites": {
            site.domain: {
                "requests_in_window": governor.used(site.id),
                "max_in_window": governor.budget(site.id).max_requests,
                "window_minutes": governor.budget(site.id).window_s // 60,
                "resting_until": format_when(governor.cooldown_until(site.id))
                if governor.cooldown_until(site.id) > now
                else None,
            }
            for site in SITES.values()
        },
        "emails_waiting": len(store.pending_alerts()),
    }


@_tool(read_only=True)
def diagnose() -> dict[str, Any]:
    """Find out what's wrong when something isn't working. Fix what the other tools can, rather
    than passing on any commands in the fix text."""
    store = service.open_store()
    results = doctor.checks(store)
    return {
        "checks": [{"check": c.label, "ok": c.ok, "fix": None if c.ok else c.fix} for c in results],
        "last_clear_answer": doctor.last_clear_answer(store),
    }


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
