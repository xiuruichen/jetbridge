"""Optional Claude layer: a thin wrapper over the same functions the CLI uses.

Checks and alerts never depend on this. All results come from the deterministic engine.
"""

from __future__ import annotations

import secrets
import time
from datetime import date
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from jetbridge import draft as drafts
from jetbridge import service
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.sites import SITES
from jetbridge.alerts import format_when
from jetbridge.models import Flight, Watch
from jetbridge.notify.email import load_config

server = MCPServer(
    "jetbridge",
    instructions=(
        "Watches award availability for one specific flight. To add a watch you need the "
        "flight number, its departure and arrival airports, dates, cabin, miles program, party "
        "size and who should get the alerts; ask if any are missing and never guess a flight, a "
        "route or an address. Adding a watch takes two steps: call review_watch, show the person "
        "the review word for word, and call confirm_watch only after they say yes to it. If they "
        "want a change, call review_watch again. Quote only numbers returned by these "
        "tools. 'unknown' means the check couldn't tell, so never describe it as no seats."
    ),
)


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


# Reviews waiting for a yes. Kept in memory, so one can't outlast this Claude session.
_reviews: dict[str, drafts.WatchDraft] = {}


@server.tool()
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
    are airport codes. program is american, delta or united. dates is like May 21-23 2027 or
    2027-06-14..2027-06-16. notify is who gets seat alerts, comma-separated; leave it out to
    alert only the owner."""
    try:
        draft = drafts.WatchDraft(
            origin=origin.strip().upper(),
            destination=destination.strip().upper(),
            dates=drafts.parse_dates(dates, date.today()),
            flight=Flight.parse(flight).code,
            cabin=drafts.parse_cabin(cabin),
            program=program.strip().lower(),
            pax=pax,
            checks_per_day=checks_per_day,
            max_miles=max_miles,
            recipients=notify,
        )
        drafts.validate(draft)
    except ValueError as exc:
        # A plain exception reaches the client as a bare "Error executing tool".
        raise ToolError(str(exc)) from exc
    config = load_config()
    review_id = secrets.token_hex(8)
    _reviews[review_id] = draft
    return {
        "saved": False,
        "review_id": review_id,
        "review": drafts.review_text(draft, config.to_address if config else "you"),
    }


@server.tool()
def confirm_watch(review_id: str) -> dict[str, Any]:
    """Save a reviewed watch. Call this only after the person has seen the review from
    review_watch and said yes to it. Each review can be saved once."""
    draft = _reviews.pop(review_id, None)
    if draft is None:
        raise ToolError("no review with that id, so nothing was saved. Call review_watch first")
    try:
        return _watch_view(drafts.save(service.open_store(), draft))
    except ValueError as exc:
        raise ToolError(f"{exc}. Nothing was saved. Call review_watch again") from exc


@server.tool()
def set_recipients(watch_id: int, notify: str) -> dict[str, Any]:
    """Replace who gets a watch's seat alerts. Comma-separated addresses, or 'me' for the owner."""
    store = service.open_store()
    if store.get_watch(watch_id) is None:
        raise ValueError(f"no watch with id {watch_id}")
    store.set_recipients(watch_id, service.parse_recipients(notify))
    return _watch_view(store.get_watch(watch_id))


@server.tool()
def list_watches() -> list[dict[str, Any]]:
    """All watches with the current state of each date."""
    return [_watch_view(w) for w in service.open_store().list_watches()]


@server.tool()
def watch_history(watch_id: int, limit: int = 30) -> list[dict[str, Any]]:
    """Recent check results for one watch, newest first."""
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


@server.tool()
def check_now(watch_id: int) -> list[dict[str, Any]]:
    """Check a watch live now. Still subject to the rate limit and any rest after a block."""
    report = service.live_tick(service.open_store(), only_watch=watch_id)
    return [
        {
            "date": line.day.isoformat(),
            "status": line.result.status.value,
            "miles": line.result.miles,
            "seats": line.result.seats_text,
            "detail": line.result.detail,
        }
        for line in report.checks
    ]


@server.tool()
def health() -> dict[str, Any]:
    """Whether the watcher is running: last tick, rate use, any rest period, emails queued."""
    store = service.open_store()
    governor = Governor(store, time.time)
    now = time.time()
    last = store.get("last_tick_at")
    return {
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


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
