"""Optional Claude layer: a thin wrapper over the same functions the CLI uses.

Checks and alerts never depend on this. All results come from the deterministic engine.
"""

from __future__ import annotations

import functools
import json
import re
import secrets
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Hashable
from concurrent import futures
from dataclasses import asdict
from datetime import date, datetime
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from jetbridge import board, doctor, headlines, power, service, updater, winsched, wizard
from jetbridge import draft as drafts
from jetbridge.acquire.governor import DAILY_LIMIT, Governor
from jetbridge.acquire.sites import SITES, parse_program
from jetbridge.alerts import DEFAULT_HEADLINES, AlertKind, format_when
from jetbridge.insights import TOO_FEW_ANSWERS, clear_answers, openings
from jetbridge.models import Cabin, CheckStatus, Flight, Query, Watch
from jetbridge.notify import email as mail
from jetbridge.notify import push
from jetbridge.runner import NEVER_SEEN_AFTER, above_limit_miles, seen_history, watch_coverage
from jetbridge.scheduler import checks_per_day_on, is_searchable, search_starts

server = MCPServer(
    "jetbridge",
    instructions=(
        "jetbridge watches award seats on one specific flight and emails when they open. "
        "Everything it can do is available through these tools, so the person never needs a "
        "command. Talk plainly: don't mention tool names, review ids, check ids, watch numbers "
        "or commands, and work out which watch they mean from list_watches. If two "
        "could match, ask which. Dates like 'next Friday' are fine: work out the date from "
        "today in get_started, and the review shows its weekday.\n\n"
        "First time, or if they ask for help getting started: call get_started and follow its "
        "next_step. Show its notice word for word and call accept_notice only after they agree. "
        "The first time Claude Desktop asks permission for a tool, mention once that they can "
        "choose Always allow, but that Allow once is safest for saving or removing a watch.\n\n"
        "Adding a watch needs the flight, its departure and arrival airports, dates, cabin, miles "
        "program, party size and who gets the alerts. Ask for anything missing and never guess. "
        "City names are fine; if a city has several airports, ask which. If they don't know the "
        "flight number, offer find_flights, then let them choose. Call review_watch, show the "
        "review word for word, and call confirm_watch only after they say yes to it. If they want "
        "a change, call review_watch again. Saving also runs the first check: tell them what "
        "first_check's means says, so a wrong flight or route shows up straight away. If it says "
        "finished false, tell them it's still going and call check_result.\n\n"
        "Changing a watch's dates, price limit or checks per day: call review_watch_change, show "
        "the review word for word, and call confirm_watch_change only after a yes. Its history is "
        "kept. Changing its flight, airports, cabin, program or party size makes it a different "
        "watch: review and confirm the new version, then offer to remove the old one. To add or "
        "remove someone from alerts, use set_recipients with action add or remove, so nobody else "
        "is dropped.\n\n"
        "When someone asks how a flight has been doing, use watch_insights, and say only what it "
        "shows: never predict when seats will open. To see everything at a glance, use show_board, "
        "which Claude Desktop draws in the chat. If list_watches shows also_seen, mention those "
        "flights as something jetbridge happened to see rather than a watch or an alert, and offer "
        "to watch one.\n\n"
        "Removing a watch takes two steps too: review_watch_removal, show what it says, and "
        "confirm_watch_removal only after a yes. Before turning off automatic checks, say that no "
        "watch will be checked until they're turned back on, and wait for a yes. Read addresses "
        "back before adding someone to alerts or sending them a test email. Say that anyone new "
        "gets one short note saying they were added. Only change the PC's sleep setting after they "
        "say yes to it.\n\n"
        "A live check or flight lookup can take a few minutes: if an answer says finished false, "
        "tell them it's still going and call check_result with its check_id until it finishes. A "
        "check can email everyone on that watch if seats have opened, so say so if they're "
        "checking a watch that alerts other people. When seats are open, give the book_at link "
        "and booking_note, and say that seats can go quickly.\n\n"
        "If get_started shows update_available, mention it once, and call install_update only "
        "after a yes. Phone alerts are optional: use open_phone_setup, which opens a private "
        "and explains that alert text passes through ntfy.sh.\n\n"
        "The Gmail app password must never go in this chat. To set up email, use open_email_setup, "
        "which opens a private window on this PC. If someone pastes a password here, tell them to "
        "revoke it in their Google account and make a new one.\n\n"
        "Quote only numbers returned by these tools, and say only what they show: the latest state "
        "of a date doesn't tell you about earlier checks, so use watch_history for that. 'unknown' "
        "means the check couldn't tell, so never describe it as no seats. A flight missing from a "
        "lookup may still exist. jetbridge can't book anything or move miles."
    ),
)


def _tool[**P, R](
    *, read_only: bool = False, meta: dict[str, Any] | None = None
) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Register a tool whose refusals reach Claude, which a plain exception's message wouldn't."""

    def register(function: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(function)
        def guarded(*args: P.args, **kwargs: P.kwargs) -> R:
            try:
                return function(*args, **kwargs)
            except (ValueError, service.TickBusy, mail.NotConfigured) as exc:
                raise ToolError(str(exc)) from exc

        server.tool(annotations=ToolAnnotations(readOnlyHint=read_only), meta=meta)(guarded)
        return function

    return register


_PACE_NOTE = (
    "checks_per_day is the watch's setting, and this is its normal pace: dates more than 60 days "
    "out get half of it, dates in the last two weeks or just after the program can first show them "
    "get double. It's a choice about where searches do the most good, not a problem to look into."
)

_BOOKING_NOTES = {
    "american": "The link goes straight to the results on aa.com.",
    "delta": "The link opens a date grid on delta.com with this date selected: press Continue.",
    "united": "united.com asks them to sign in before it shows prices in miles.",
}


def _booking(query: Query) -> dict[str, str]:
    return {
        "book_at": SITES[query.program].search_url(query),
        "booking_note": _BOOKING_NOTES[query.program],
    }


def _watch_query(watch: Watch, day: date) -> Query:
    return Query(watch.origin, watch.destination, day, watch.pax, watch.program)


def _watch_view(watch: Watch) -> dict[str, Any]:
    store = service.open_store()
    today = date.today()
    day_ago = time.time() - 86400
    days = []
    never_seen = True
    for day in watch.dates:
        state = store.date_state(watch.id, day)
        unopened = day >= today and not is_searchable(day, watch.program, today)
        if day >= today and (
            unopened or state.ever_listed or state.conclusive_count < NEVER_SEEN_AFTER
        ):
            never_seen = False
        view: dict[str, Any] = {
            "date": day.isoformat(),
            "status": state.status.value if state.status else "no clear answer yet",
            "miles": state.miles,
            "last_clear_answer": format_when(state.last_conclusive_at)
            if state.last_conclusive_at
            else None,
            "clear_answers_so_far": state.conclusive_count,
            "date_has_passed": day < today,
        }
        if day >= today and not unopened:
            view["checks_per_day_now"] = checks_per_day_on(
                day, watch.program, watch.checks_per_day, today
            )
        over = above_limit_miles(store, watch, day) if day >= today else None
        if over:
            view["seats_above_price_limit_at_miles"] = over
        others = store.get_also_seen(watch.id, day) if day >= today else None
        if others and state.status is not CheckStatus.AVAILABLE:
            view["also_seen"] = {
                "note": "Other flights with seats in this cabin. jetbridge isn't watching these, "
                "and the list may be incomplete.",
                "as_of": format_when(others["at"]),
                "flights": [
                    {"flight": o["flight"], "miles": o["miles"]} for o in others["flights"]
                ],
                **_booking(_watch_query(watch, day)),
            }
        if unopened:
            view["status"] = "not searched yet"
            view["searching_starts"] = search_starts(day, watch.program).strftime("%b %d, %Y")
        if state.status is CheckStatus.AVAILABLE and day >= today:
            view.update(_booking(_watch_query(watch, day)))
        days.append(view)
    extra: dict[str, Any] = {}
    watched = watch_coverage(store, watch, time.time()) if watch.status == "active" else None
    if watched is not None:
        extra["proof_of_watching"] = watched.text
    if never_seen and any(day >= today for day in watch.dates):
        domain = SITES[watch.program].domain
        extra["note"] = (
            f"{watch.flight.code} has never appeared in {domain}'s results for this route. The "
            f"flight number or route may be wrong, {domain} may not sell this flight with these "
            "miles, or it may just hide flights with no award space."
        )
    return {
        "id": watch.id,
        "flight": watch.flight.code,
        "route": f"{watch.origin}-{watch.destination}",
        "cabin": watch.cabin.value,
        "program": watch.program,
        "miles_program": SITES[watch.program].label,
        "pax": watch.pax,
        "checks_per_day": watch.checks_per_day,
        "pace_note": _PACE_NOTE,
        "max_miles": watch.max_miles,
        "seat_alerts_to": list(watch.recipients) or [_owner_address() or "the owner"],
        "status": watch.status,
        "next_check": format_when(watch.next_check_at) if watch.status == "active" else None,
        "last_24_hours": {
            "clear_answers": sum(
                store.count_recent(watch.id, status, day_ago)
                for status in CheckStatus
                if status.conclusive
            ),
            "inconclusive": store.count_recent(watch.id, CheckStatus.UNKNOWN, day_ago),
        },
        "dates": days,
        **extra,
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
        "today": date.today().strftime("%A, %B %d, %Y"),
        "notice_accepted": accepted,
        "email_set_up": email,
        "alerts_go_to": _owner_address(),
        "automatic_checks_on": checks_on,
        "watches": watches,
        "next_step": next_step,
    }
    if not accepted:
        answer["notice"] = service.RISK_NOTICE
    newer = updater.newer_version(store)
    if newer:
        answer["update_available"] = newer
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
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jetbridge", "notify", "setup-window"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        raise ToolError(f"the window couldn't open: {exc}") from exc
    return {
        "message": "A window opened on this PC, possibly behind this one. It asks for the Gmail "
        "address to send from, the address alerts go to, and the app password, then sends a test "
        "email. Ask them to say when it's done, then call get_started to confirm."
    }


@_tool()
def open_phone_setup() -> dict[str, str]:
    """Open a private window on this PC to turn on optional phone alerts through ntfy.

    The private channel name stays in that window and never goes in the chat. It explains what
    passes through ntfy.sh and asks before turning anything on, then sends a test."""
    try:
        subprocess.Popen(
            [sys.executable, "-m", "jetbridge", "notify", "push-window"],
            creationflags=subprocess.CREATE_NEW_CONSOLE,
        )
    except OSError as exc:
        raise ToolError(f"the window couldn't open: {exc}") from exc
    return {
        "message": "A window opened on this PC, possibly behind this one. It explains how phone "
        "alerts work and asks before turning them on. Ask them to say when it's done, then call "
        "alert_settings to confirm."
    }


@_tool()
def turn_off_phone_alerts() -> dict[str, bool]:
    """Stop phone alerts. Emails carry on."""
    push.turn_off()
    return {"phone_alerts_on": False}


@_tool()
def install_update() -> dict[str, Any]:
    """Install the newest jetbridge. Call this only after the person says yes. It can take a
    minute or two: if finished is false, call check_result with its check_id. They need to restart
    Claude Desktop afterward. Checks and watches aren't affected."""

    def work() -> dict[str, Any]:
        try:
            return {"updated": True, "report": updater.install()}
        except updater.UpdateProblem as exc:
            return {"updated": False, "problem": str(exc)}

    return _start_search(("update",), work)


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
# Reviews waiting for a yes. Claude Desktop can run two copies of this server and restart them, so
# these are saved where every copy can see them, and go stale after a day.
_REVIEW = "chat_review:"
_REMOVAL = "chat_removal:"
_CHANGE = "chat_change:"
_REVIEW_LIFETIME_S = 86400


def _remember(prefix: str, payload: dict[str, Any]) -> str:
    store = service.open_store()
    now = time.time()
    pending = (*store.keys_with_prefix(_REVIEW), *store.keys_with_prefix(_REMOVAL))
    for key in (*pending, *store.keys_with_prefix(_CHANGE)):
        saved = json.loads(store.get(key) or "{}")
        if now - saved.get("saved_at", 0) > _REVIEW_LIFETIME_S:
            store.delete(key)
    token = secrets.token_hex(8)
    store.set(prefix + token, json.dumps({"saved_at": now, **payload}))
    return token


def _recall(prefix: str, token: str) -> dict[str, Any] | None:
    """Each review works once, and not after it has gone stale."""
    text = service.open_store().take(prefix + token)
    if text is None:
        return None
    saved = json.loads(text)
    if time.time() - saved["saved_at"] > _REVIEW_LIFETIME_S:
        return None
    return saved


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
    fields = {**asdict(draft), "cabin": draft.cabin.value}
    review_id = _remember(_REVIEW, {"draft": fields})
    return {
        "saved": False,
        "review_id": review_id,
        "review": drafts.review_text(draft, _owner_address() or "you"),
    }


@_tool()
def confirm_watch(review_id: str) -> dict[str, Any]:
    """Save a reviewed watch, then run its first check. Call this only after the person has seen
    the review from review_watch and said yes to it. Each review can be saved once. Automatic checks
    are turned on if they aren't already. first_check has the answer, including what it means. If
    its finished is false, call check_result with its check_id."""
    _require_notice()
    saved = _recall(_REVIEW, review_id)
    if saved is None:
        raise ToolError("no review with that id, so nothing was saved. Call review_watch first")
    fields = saved["draft"]
    draft = drafts.WatchDraft(**{**fields, "cabin": Cabin(fields["cabin"])})
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
    try:
        first_check = _start_search(("check", watch.id), lambda: _check_results(watch))
    except ToolError as exc:  # the watch is saved, and scheduled checks will still run
        first_check = {"finished": True, "problem": f"{exc}. A scheduled check will try again."}
    updated = _require_watch(watch.id)
    return {**_watch_view(updated), "first_check": first_check, "next_steps": next_steps}


# ---- long searches --------------------------------------------------------------------
# Claude Desktop gives up on a tool call at about 60 seconds, and a live check can take longer
# because Chrome has to start and load the airline's site. So each one runs on its own thread and
# each call waits for less than that.
CHECK_WAIT_S = 50.0
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


def _check_results(watch: Watch) -> dict[str, Any]:
    report = service.live_tick(service.open_store(), only_watch=watch.id)
    # An opening is checked twice before its alert goes out. The later answer is current.
    latest = {line.day: line for line in report.checks}
    results = []
    for line in latest.values():
        row = {
            "date": line.day.isoformat(),
            "status": line.result.status.value,
            "miles": line.result.miles,
            "seats": line.result.seats_text,
            "detail": line.result.detail,
            "means": service.first_check_means(watch, line.result),
        }
        if line.result.status is CheckStatus.AVAILABLE:
            row.update(_booking(_watch_query(watch, line.day)))
        results.append(row)
    today = date.today()
    return {
        "results": results,
        "not_searched_yet": [
            {
                "date": day.isoformat(),
                "searching_starts": search_starts(day, watch.program).strftime("%b %d, %Y"),
            }
            for day in watch.dates
            if day >= today and not is_searchable(day, watch.program, today)
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
    return _start_search(("check", watch_id), lambda: _check_results(watch))


@_tool(read_only=True)
def check_result(check_id: str) -> dict[str, Any]:
    """Get the answer to a check_now or find_flights that was still running. Waits up to 50 seconds,
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
            **(_booking(query) if option.result.status is CheckStatus.AVAILABLE else {}),
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


def _with_owner_named(text: str, owner: str | None) -> str:
    if not owner:
        return text
    return re.sub(r"(?<![^\s,;])me(?![^\s,;])", owner, text, flags=re.IGNORECASE)


@_tool()
def set_recipients(watch_id: int, notify: str, action: str = "replace") -> dict[str, Any]:
    """Change who gets a watch's seat alerts. notify is comma-separated addresses, where 'me' means
    the owner. action is add, remove or replace. Use add for "also alert Ann", so nobody is dropped.
    If nobody would be left, the owner gets the alerts."""
    watch = _require_watch(watch_id)
    owner = _owner_address()
    named = list(service.parse_recipients(_with_owner_named(notify, owner)))
    current = list(watch.recipients) or ([owner] if owner else [])
    if action == "add":
        wanted = current + [address for address in named if address not in current]
    elif action == "remove":
        wanted = [address for address in current if address not in named]
    elif action == "replace":
        wanted = named
    else:
        raise ToolError("action must be add, remove or replace")
    # A list of just the owner is stored as empty, which is how a watch says "the owner".
    recipients = () if wanted == [owner] else service.parse_recipients(",".join(wanted))
    updated, welcomed = service.set_recipients(service.open_store(), watch, recipients)
    return {**_watch_view(updated), "will_get_a_short_note": welcomed}


@server.resource(board.URI, name="jetbridge board", mime_type=board.MIME_TYPE)
def board_page() -> str:
    return board.PAGE


@_tool(read_only=True, meta={"ui": {"resourceUri": board.URI}})
def show_board() -> dict[str, Any]:
    """Show every watch at a glance: its dates, what's open, proof of watching and past openings.
    Claude Desktop draws this in the chat. Elsewhere, this is the same information as data."""
    store = service.open_store()
    now = time.time()
    watches = []
    for watch in store.list_watches():
        if watch.status == "finished":
            continue
        view = _watch_view(watch)
        for row, day in zip(view["dates"], watch.dates, strict=True):
            found = openings(seen_history(store, watch.id, day))
            row["openings"] = [
                {
                    "first_seen": format_when(past.first_seen),
                    "how_long": past.lasted(now),
                    "lowest_miles": past.lowest_miles,
                }
                for past in found
            ]
        watches.append(view)
    return {
        "watches": watches,
        "note": "The person can already see these on the board above your reply, so don't list "
        "them again. In a sentence or two, add only what they'd want to act on.",
    }


@_tool(read_only=True)
def watch_insights(watch_id: int) -> dict[str, Any]:
    """What jetbridge has seen for a watch so far: each opening, how long it lasted, its price, and
    how far ahead of departure it was. Only what was seen at its checks, not a prediction."""
    watch = _require_watch(watch_id)
    store = service.open_store()
    now = time.time()
    days = []
    for day in watch.dates:
        history = seen_history(store, watch_id, day)
        days.append(
            {
                "date": day.isoformat(),
                "clear_answers": clear_answers(history),
                "openings": [
                    {
                        "first_seen": format_when(found.first_seen),
                        "how_long": found.lasted(now),
                        "lowest_miles": found.lowest_miles,
                        "highest_miles": found.highest_miles,
                        "days_before_departure": (
                            day - datetime.fromtimestamp(found.first_seen).date()
                        ).days,
                    }
                    for found in openings(history)
                ],
            }
        )
    answers = sum(day["clear_answers"] for day in days)
    watched = watch_coverage(store, watch, now) if watch.status == "active" else None
    return {
        "flight": watch.label,
        "proof_of_watching": watched.text if watched else None,
        "pace_note": _PACE_NOTE,
        "dates": days,
        "enough_to_go_on": answers >= TOO_FEW_ANSWERS,
        "note": "Only what jetbridge saw at its checks, which are hours apart. Short openings "
        "between checks aren't here. Don't use this to predict when seats will open.",
    }


@_tool()
def review_watch_change(
    watch_id: int,
    dates: str | None = None,
    max_miles: int | None = None,
    remove_price_limit: bool = False,
    checks_per_day: int | None = None,
) -> dict[str, Any]:
    """Show how a watch would look with new dates, a new price limit or a new pace. This changes
    nothing. Leave out anything that stays the same. dates replaces all of its dates, so include any
    being kept. Show the review word for word and ask them to confirm it."""
    watch = _require_watch(watch_id)
    if max_miles is not None and remove_price_limit:
        raise ToolError("that sets a price limit and removes it. Ask which they want")
    new_dates = (
        drafts.parse_dates(dates, date.today())
        if dates
        else ",".join(day.isoformat() for day in watch.dates)
    )
    pace = checks_per_day if checks_per_day is not None else watch.checks_per_day
    limit = (
        None if remove_price_limit else (max_miles if max_miles is not None else watch.max_miles)
    )
    draft = drafts.from_watch(watch, new_dates, pace, limit)
    drafts.validate(draft)
    change_id = _remember(
        _CHANGE,
        {"watch_id": watch_id, "dates": new_dates, "checks_per_day": pace, "max_miles": limit},
    )
    return {
        "changed": False,
        "change_id": change_id,
        "review": drafts.review_text(draft, _owner_address() or "you", editing=True),
        "note": "Its history is kept.",
    }


@_tool()
def confirm_watch_change(change_id: str) -> dict[str, Any]:
    """Save a change from review_watch_change. Call this only after the person has seen that
    review and said yes to it. Each review can be saved once."""
    saved = _recall(_CHANGE, change_id)
    if saved is None:
        raise ToolError("no review with that id, so nothing changed. Call review_watch_change")
    watch = _require_watch(saved["watch_id"])
    try:
        updated = service.change_watch(
            service.open_store(),
            watch,
            dates=saved["dates"],
            checks_per_day=saved["checks_per_day"],
            max_miles=saved["max_miles"],
        )
    except ValueError as exc:
        raise ToolError(f"{exc}. Nothing changed. Call review_watch_change again") from exc
    return _watch_view(updated)


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
    removal_id = _remember(_REMOVAL, {"watch_id": watch_id})
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
    saved = _recall(_REMOVAL, removal_id)
    if saved is None:
        raise ToolError("no removal with that id, so nothing changed. Call review_watch_removal")
    watch = _require_watch(saved["watch_id"])
    service.open_store().delete_watch(watch.id)
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
        "phone_alerts_on": push.enabled(),
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
OVERDUE_S = 3600  # the scheduled task runs every 15 minutes


@_tool()
def set_keep_awake(on: bool) -> dict[str, Any]:
    """Stop this PC sleeping while it's plugged in, so checks keep running, or put its sleep
    settings back. Battery settings never change. Call this only after the person says yes."""
    store = service.open_store()
    try:
        if on:
            power.keep_awake(store)
        else:
            power.restore(store)
    except OSError as exc:
        raise ToolError(f"Windows wouldn't change that: {exc}") from exc
    timers = power.plugged_in_timers()
    return {"stays_awake_while_plugged_in": bool(timers and timers.stays_awake)}


@_tool(read_only=True)
def health() -> dict[str, Any]:
    """Whether the watcher is running: last tick, rate use, any rest period, emails queued."""
    store = service.open_store()
    governor = Governor(store, time.time)
    now = time.time()
    last = store.get("last_tick_at")
    checks_on = winsched.is_installed()
    problems = [
        f"{check.label}: {check.chat_fix or check.fix}"
        for check in doctor.checks(store)
        if not check.ok and check.serious
    ]
    if checks_on and last and now - float(last) > OVERDUE_S:
        problems.append(
            f"Checks last ran {format_when(float(last))}. They only run while this PC is on, awake "
            "and logged in."
        )
    for site in SITES.values():
        if governor.cooldown_until(site.id) > now:
            problems.append(
                f"{site.domain} is resting after a block until "
                f"{format_when(governor.cooldown_until(site.id))}, so it isn't being checked. "
                "This is normal and clears by itself."
            )
    return {
        "healthy": not problems,
        "problems": problems,
        "automatic_checks_on": checks_on,
        "last_tick": format_when(float(last)) if last else None,
        "sites": {
            site.domain: {
                "requests_in_window": governor.used(site.id),
                "max_in_window": governor.budget(site.id).max_requests,
                "window_minutes": governor.budget(site.id).window_s // 60,
                "requests_last_24_hours": governor.used_today(site.id),
                "max_per_24_hours": DAILY_LIMIT,
                "resting_until": format_when(governor.cooldown_until(site.id))
                if governor.cooldown_until(site.id) > now
                else None,
            }
            for site in SITES.values()
        },
        "emails_waiting": sum(
            item.recipient != push.PUSH_RECIPIENT for item in store.pending_alerts()
        ),
    }


@_tool(read_only=True)
def diagnose() -> dict[str, Any]:
    """Find out what's wrong when something isn't working, with how to fix each thing."""
    store = service.open_store()
    results = doctor.checks(store)
    return {
        "checks": [
            {"check": c.label, "ok": c.ok, "fix": None if c.ok else (c.chat_fix or c.fix)}
            for c in results
        ],
        "last_clear_answer": doctor.last_clear_answer(store),
    }


# ---- starting points, shown in Claude's + menu ------------------------------------------
@server.prompt(title="Get started with jetbridge")
def get_started_prompt() -> str:
    return "Help me get started with jetbridge."


@server.prompt(title="Watch a flight")
def watch_a_flight_prompt() -> str:
    return "I'd like to watch a flight for award seats. Ask me what you need to know."


@server.prompt(title="How are my flights doing?")
def my_flights_prompt() -> str:
    return "How are the flights I'm watching doing, and is everything working?"


@server.prompt(title="Something seems wrong")
def fix_a_problem_prompt() -> str:
    return "Something doesn't seem right with jetbridge. Can you find out what's wrong?"


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
