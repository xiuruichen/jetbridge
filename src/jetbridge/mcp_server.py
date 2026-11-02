"""Optional Claude layer: a thin wrapper over the same functions the CLI uses.

Checks and alerts never depend on this. All results come from the deterministic engine.
"""

from __future__ import annotations

import time
from typing import Any

from mcp.server.mcpserver import MCPServer

from jetbridge import service
from jetbridge.acquire import american
from jetbridge.acquire.governor import Governor
from jetbridge.alerts import format_when
from jetbridge.models import Watch

server = MCPServer(
    "jetbridge",
    instructions=(
        "Watches award availability for one specific flight. To add a watch you need the "
        "flight number, its departure and arrival airports, dates, cabin and party size; ask "
        "if any are missing and never guess a route. Quote only numbers returned by these "
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
        "status": watch.status,
        "next_check": format_when(watch.next_check_at),
        "dates": days,
    }


@server.tool()
def add_watch(
    flight: str,
    origin: str,
    destination: str,
    dates: str,
    cabin: str = "business",
    pax: int = 1,
    checks_per_day: int = 6,
    max_miles: int | None = None,
) -> dict[str, Any]:
    """Start watching a flight. dates is like 2027-05-21..2027-05-23 or a comma-separated list."""
    watch = service.create_watch(
        service.open_store(),
        flight=flight,
        origin=origin,
        destination=destination,
        dates=dates,
        cabin=cabin,
        pax=pax,
        checks_per_day=checks_per_day,
        max_miles=max_miles,
    )
    return _watch_view(watch)


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
    until = governor.cooldown_until(american.SITE)
    last = store.get("last_tick_at")
    return {
        "last_tick": format_when(float(last)) if last else None,
        "requests_last_10_min": governor.used(american.SITE),
        "resting_until": format_when(until) if until > time.time() else None,
        "emails_waiting": len(store.pending_alerts()),
    }


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()
