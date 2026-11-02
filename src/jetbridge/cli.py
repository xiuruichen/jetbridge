"""Command line interface."""

from __future__ import annotations

import shutil
import time
from datetime import datetime
from pathlib import Path

import typer

from jetbridge import service, winsched
from jetbridge.acquire import american
from jetbridge.acquire.governor import Governor
from jetbridge.alerts import format_when
from jetbridge.models import CheckStatus, Watch
from jetbridge.notify import email as mail
from jetbridge.paths import home, raw_dir
from jetbridge.store import Store

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Watch award seats on a flight.")
watch_app = typer.Typer(no_args_is_help=True, help="Add and manage watches.")
notify_app = typer.Typer(no_args_is_help=True, help="Set up and test email alerts.")
schedule_app = typer.Typer(no_args_is_help=True, help="Run checks automatically on this PC.")
app.add_typer(watch_app, name="watch")
app.add_typer(notify_app, name="notify")
app.add_typer(schedule_app, name="schedule")


def _fail(message: str) -> typer.Exit:
    typer.echo(f"Error: {message}", err=True)
    return typer.Exit(code=1)


def _require(store: Store, watch_id: int) -> Watch:
    watch = store.get_watch(watch_id)
    if watch is None:
        raise _fail(f"no watch with id {watch_id}")
    return watch


def _describe(watch: Watch) -> str:
    days = ", ".join(d.strftime("%b %d %Y") for d in watch.dates)
    limit = f", max {watch.max_miles:,} miles" if watch.max_miles else ""
    return (
        f"[{watch.id}] {watch.label} via {watch.program}, {watch.pax} pax, "
        f"{watch.checks_per_day}/day{limit} ({watch.status})\n    Dates: {days}"
    )


# ---- watch ----------------------------------------------------------------------------
@watch_app.command("add")
def watch_add(
    flight: str = typer.Argument(..., help="Flight number, e.g. CX805"),
    origin: str = typer.Option(..., "--from", help="Departure airport of this flight"),
    destination: str = typer.Option(..., "--to", help="Arrival airport of this flight"),
    dates: str = typer.Option(..., help="2027-05-21..2027-05-23 or 2027-05-21,2027-05-23"),
    cabin: str = typer.Option("business", help="economy, premium, business or first"),
    program: str = typer.Option("american", help="Program to search"),
    pax: int = typer.Option(1, help="Party size; searched at this real size"),
    checks_per_day: int = typer.Option(6, help="Checks per day, per date"),
    max_miles: int | None = typer.Option(None, help="Only alert at or below this price"),
) -> None:
    """Start watching a flight. The first check runs on the next tick."""
    store = service.open_store()
    try:
        watch = service.create_watch(
            store,
            flight=flight,
            origin=origin,
            destination=destination,
            dates=dates,
            cabin=cabin,
            program=program,
            pax=pax,
            checks_per_day=checks_per_day,
            max_miles=max_miles,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(_describe(watch))
    typer.echo(f"Run 'jetbridge check {watch.id}' to check it now.")


@watch_app.command("list")
def watch_list() -> None:
    """List all watches."""
    watches = service.open_store().list_watches()
    if not watches:
        typer.echo("No watches yet. Add one with: jetbridge watch add")
    for watch in watches:
        typer.echo(_describe(watch))


@watch_app.command("pause")
def watch_pause(watch_id: int) -> None:
    store = service.open_store()
    _require(store, watch_id)
    store.set_watch_status(watch_id, "paused")
    typer.echo(f"Paused watch {watch_id}.")


@watch_app.command("resume")
def watch_resume(watch_id: int) -> None:
    store = service.open_store()
    _require(store, watch_id)
    store.set_watch_status(watch_id, "active")
    store.set_next_check(watch_id, time.time())
    typer.echo(f"Resumed watch {watch_id}.")


@watch_app.command("rm")
def watch_rm(watch_id: int) -> None:
    """Delete a watch and its history."""
    store = service.open_store()
    _require(store, watch_id)
    store.delete_watch(watch_id)
    typer.echo(f"Removed watch {watch_id}.")


# ---- running --------------------------------------------------------------------------
def _print_report(report) -> None:
    for line in report.checks:
        result = line.result
        text = result.status.value
        if result.status is CheckStatus.AVAILABLE:
            text += f": {result.miles:,} miles, {result.seats_text} seats"
        elif result.detail:
            text += f" ({result.detail})"
        typer.echo(f"[{line.watch_id}] {line.label} {line.day:%b %d}: {text}")
    if report.stopped_early:
        typer.echo(f"Stopped early: {report.stopped_early}")
    typer.echo(f"{len(report.checks)} check(s), {report.alerts_sent} email(s) sent.")


@app.command()
def tick() -> None:
    """Run whatever is due. The scheduled task calls this every 15 minutes."""
    store = service.open_store()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        report = service.live_tick(store)
    except service.TickBusy as exc:
        typer.echo(f"{stamp} skipped: {exc}")
        return
    if report.checks or report.alerts_sent:
        typer.echo(stamp)
        _print_report(report)


@app.command()
def check(watch_id: int) -> None:
    """Check one watch now, whatever its schedule. Still goes through the rate governor."""
    store = service.open_store()
    watch = _require(store, watch_id)
    if watch.status != "active":
        raise _fail(f"watch {watch_id} is {watch.status}")
    try:
        _print_report(service.live_tick(store, only_watch=watch_id))
    except service.TickBusy as exc:
        raise _fail(str(exc)) from exc


@app.command()
def status() -> None:
    """Current state of every active watch, and of the watcher itself."""
    store = service.open_store()
    watches = store.list_watches("active")
    today = datetime.now().date()
    if not watches:
        typer.echo("No active watches.")
    for watch in watches:
        typer.echo(_describe(watch))
        for day in watch.dates:
            if day < today:
                continue
            state = store.date_state(watch.id, day)
            text = state.status.value if state.status else "no clear answer yet"
            price = f", {state.miles:,} miles" if state.miles else ""
            seen = format_when(state.last_conclusive_at) if state.last_conclusive_at else "never"
            typer.echo(f"    {day:%b %d}: {text}{price} (last clear answer: {seen})")
        typer.echo(f"    Next check: {format_when(watch.next_check_at)}")
    _print_health(store)


def _print_health(store: Store) -> None:
    governor = Governor(store, time.time)
    until = governor.cooldown_until(american.SITE)
    typer.echo(f"aa.com requests in the last 10 minutes: {governor.used(american.SITE)}/10")
    if until > time.time():
        typer.echo(f"aa.com is RESTING after a block until {format_when(until)}.")
    last = store.get("last_tick_at")
    typer.echo(f"Last tick: {format_when(float(last)) if last else 'never'}")
    pending = store.pending_alerts()
    if pending:
        typer.echo(f"Emails waiting to send: {len(pending)}")


@app.command()
def history(watch_id: int, limit: int = 30) -> None:
    """Recent check results for a watch, newest first."""
    store = service.open_store()
    _require(store, watch_id)
    for row in store.history(watch_id, limit):
        price = f" {row['miles']:,} miles, seats: {row['seats']}" if row["miles"] else ""
        detail = f" ({row['detail']})" if row["detail"] else ""
        typer.echo(
            f"{format_when(row['observed_at'])}  {row['day']}  {row['status']}{price}{detail}"
        )


@app.command()
def doctor() -> None:
    """Check that everything needed to watch and alert is working."""
    store = service.open_store()
    chrome = shutil.which("chrome") or next(
        (
            p
            for p in (
                r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
            )
            if Path(p).exists()
        ),
        None,
    )
    config = mail.load_config()
    password = (
        mail.keyring.get_password(mail.KEYRING_SERVICE, config.from_address) if config else None
    )
    good = store.conn.execute(
        "SELECT MAX(observed_at) AS t FROM observations WHERE status!=?",
        (CheckStatus.UNKNOWN.value,),
    ).fetchone()["t"]
    failures = [
        row["last_error"]
        for row in store.conn.execute(
            "SELECT last_error FROM outbox WHERE sent_at IS NULL AND last_error IS NOT NULL"
        )
    ]
    checks = [
        ("Google Chrome installed", bool(chrome), "Install Google Chrome"),
        ("Email set up", bool(config and password), "Run: jetbridge notify setup"),
        ("Emails sending", not failures, failures[-1] if failures else ""),
        ("Scheduled task installed", winsched.is_installed(), "Run: jetbridge schedule install"),
        (
            "At least one active watch",
            bool(store.list_watches("active")),
            "Run: jetbridge watch add",
        ),
        ("A clear answer from aa.com so far", good is not None, "Run: jetbridge check <id>"),
    ]
    for label, ok, fix in checks:
        typer.echo(f"[{' OK ' if ok else 'FAIL'}] {label}" + ("" if ok else f" -> {fix}"))
    if good is not None:
        typer.echo(f"Last clear answer: {format_when(good)}")
    _print_health(store)
    typer.echo(f"State folder: {home()}")
    typer.echo(f"Saved failed responses: {len(list(raw_dir().glob('*.html')))}")


# ---- notify ---------------------------------------------------------------------------
@notify_app.command("setup")
def notify_setup() -> None:
    """Save your Gmail address and app password. The password goes in Credential Manager."""
    typer.echo("Create an app password at https://myaccount.google.com/apppasswords")
    typer.echo("(this needs 2-Step Verification on the Google account).")
    sender = typer.prompt("Send from (Gmail address)").strip()
    to = typer.prompt("Send alerts to", default=sender).strip()
    password = typer.prompt("Gmail app password (hidden)", hide_input=True).strip()
    mail.save_config(mail.EmailConfig(sender, to), password)
    typer.echo("Saved. Sending a test email...")
    notify_test()


@notify_app.command("test")
def notify_test() -> None:
    """Send a test email now."""
    try:
        mail.GmailSender().send(
            "jetbridge test email", "If you can read this, jetbridge alerts will reach you."
        )
    except Exception as exc:
        raise _fail(f"couldn't send: {exc}") from exc
    typer.echo("Sent. Check your inbox, and your spam folder if it isn't there.")


@notify_app.command("digest")
def notify_digest(state: str = typer.Argument(..., help="on or off")) -> None:
    """Turn the daily summary email on or off."""
    if state not in ("on", "off"):
        raise _fail("use 'on' or 'off'")
    service.open_store().set("digest_enabled", "1" if state == "on" else "0")
    typer.echo(f"Daily digest {state}.")


# ---- schedule -------------------------------------------------------------------------
@schedule_app.command("install")
def schedule_install() -> None:
    """Create the Windows scheduled task that runs a tick every 15 minutes."""
    try:
        winsched.install()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Installed task '{winsched.TASK_NAME}'. It runs while you're logged on.")


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Remove the scheduled task. Watches are kept."""
    try:
        winsched.remove()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.echo("Removed the scheduled task.")
