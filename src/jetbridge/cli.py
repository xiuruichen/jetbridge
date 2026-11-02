"""Command line interface."""

from __future__ import annotations

import shutil
import time
from datetime import date, datetime
from pathlib import Path

import typer

from jetbridge import headlines, service, winsched, wizard
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.sites import SITES
from jetbridge.alerts import DEFAULT_HEADLINES, AlertKind, format_when
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
        f"{watch.checks_per_day}/day{limit} ({watch.status})\n    Dates: {days}\n"
        f"    Seat alerts to: {_recipients_text(watch)}"
    )


def _recipients_text(watch: Watch) -> str:
    if watch.recipients:
        return ", ".join(watch.recipients)
    owner = mail.load_config()
    return f"you ({owner.to_address})" if owner else "you (email isn't set up yet)"


def _ask_recipients() -> str:
    """Ask who gets this watch's alerts. With no one to answer, stop rather than guess."""
    owner = mail.load_config()
    who = owner.to_address if owner else "you"
    try:
        return typer.prompt(
            f"Send seat alerts to (comma-separated; Enter for {who})",
            default="",
            show_default=False,
        )
    except typer.Abort as exc:
        raise _fail(
            "no recipient given, so no watch was added. Use --notify a@x.com or me"
        ) from exc


# ---- watch ----------------------------------------------------------------------------
@watch_app.command("add")
def watch_add(
    flight: str = typer.Argument(..., help="Flight number, e.g. AA100"),
    origin: str = typer.Option(..., "--from", help="Departure airport of this flight"),
    destination: str = typer.Option(..., "--to", help="Arrival airport of this flight"),
    dates: str = typer.Option(..., help="2027-06-14..2027-06-16 or 2027-06-14,2027-06-16"),
    cabin: str = typer.Option("business", help="economy, premium, business or first"),
    program: str = typer.Option("american", help="Miles to search: american, delta or united"),
    pax: int = typer.Option(1, help="Party size; searched at this real size"),
    checks_per_day: int = typer.Option(6, help="Checks per day, per date"),
    max_miles: int | None = typer.Option(None, help="Only alert at or below this price"),
    notify: str | None = typer.Option(
        None, help="Who gets seat alerts: a@x.com,b@y.com or 'me'. Prompts if left out"
    ),
) -> None:
    """Start watching a flight. The first check runs on the next tick."""
    store = service.open_store()
    try:
        service.parse_dates(dates)  # catch a bad date before asking anything else
        if notify is None:
            notify = _ask_recipients()
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
            recipients=notify,
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(_describe(watch))
    typer.echo(f"Run 'jetbridge check {watch.id}' to check it now.")


@watch_app.command("recipients")
def watch_recipients(
    watch_id: int,
    addresses: str | None = typer.Argument(
        None, help="a@x.com,b@y.com replaces the list; 'me' means only you; omit to show it"
    ),
) -> None:
    """Show or replace who gets a watch's seat alerts."""
    store = service.open_store()
    watch = _require(store, watch_id)
    if addresses is not None:
        try:
            store.set_recipients(watch_id, service.parse_recipients(addresses))
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        watch = _require(store, watch_id)
    typer.echo(f"Seat alerts for watch {watch_id} go to: {_recipients_text(watch)}")


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
            text += f": {result.miles:,} miles, seats: {result.seats_text}"
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
    watched = {watch.program for watch in store.list_watches("active")}
    for site in SITES.values():
        used, until = governor.used(site.id), governor.cooldown_until(site.id)
        resting = until > time.time()
        if site.id in watched or used or resting:
            budget = governor.budget(site.id)
            minutes = budget.window_s // 60
            typer.echo(
                f"{site.domain} requests in the last {minutes} minutes: "
                f"{used}/{budget.max_requests}"
            )
        if resting:
            typer.echo(f"{site.domain} is RESTING after a block until {format_when(until)}.")
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
    headline_problem = headlines.problem()
    checks = [
        ("Google Chrome installed", bool(chrome), "Install Google Chrome"),
        ("Email set up", bool(config and password), "Run: jetbridge notify setup"),
        ("Emails sending", not failures, failures[-1] if failures else ""),
        ("Alert headlines readable", headline_problem is None, headline_problem or ""),
        ("Scheduled task installed", winsched.is_installed(), "Run: jetbridge schedule install"),
        (
            "At least one active watch",
            bool(store.list_watches("active")),
            "Run: jetbridge watch add",
        ),
        ("A clear answer from an airline so far", good is not None, "Run: jetbridge check <id>"),
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
    notify_test(to=None)


@notify_app.command("test")
def notify_test(
    to: str | None = typer.Option(None, help="Send to this address instead of your own"),
) -> None:
    """Send a test email now, to you or to someone you're adding."""
    try:
        address = service.parse_recipients(to)
        if len(address) > 1:
            raise ValueError("test one address at a time")
        mail.GmailSender().send(
            "jetbridge test email",
            "If you can read this, jetbridge alerts will reach you.",
            address[0] if address else None,
        )
    except Exception as exc:
        raise _fail(f"couldn't send: {exc}") from exc
    typer.echo("Sent. Check the inbox, and the spam folder if it isn't there.")


@notify_app.command("digest")
def notify_digest(state: str = typer.Argument(..., help="on or off")) -> None:
    """Turn the daily summary email on or off."""
    if state not in ("on", "off"):
        raise _fail("use 'on' or 'off'")
    service.open_store().set("digest_enabled", "1" if state == "on" else "0")
    typer.echo(f"Daily digest {state}.")


@notify_app.command("headline")
def notify_headline(
    kind: str | None = typer.Argument(None, help="Which alert, e.g. seats_open"),
    text: str | None = typer.Argument(None, help="The new headline"),
    reset: bool = typer.Option(False, "--reset", help="Go back to the built-in headline"),
) -> None:
    """Show or change the headline at the start of each seat alert's subject."""
    custom = headlines.load()
    if kind is None:
        for alert in AlertKind:
            typer.echo(f"{alert.value}: {custom.get(alert, DEFAULT_HEADLINES[alert])}")
        return
    problem = headlines.problem()
    if problem:
        raise _fail(f"fix or delete this file first: {problem}")
    if reset == (text is not None):
        raise _fail("give the new headline or --reset, but not both")
    try:
        alert = AlertKind(kind)
        if text is None:
            custom.pop(alert, None)
        else:
            custom[alert] = headlines.clean(text)
    except ValueError as exc:
        raise _fail(f"{exc}. Run 'jetbridge notify headline' to see the alerts") from exc
    headlines.save(custom)
    typer.echo(f"{alert.value}: {custom.get(alert, DEFAULT_HEADLINES[alert])}")


# ---- start ----------------------------------------------------------------------------
_EMAIL_FAILED = "email_test_failed"
_RISK_ACCEPTED = "risk_accepted"
_RISK_NOTICE = """Before you start, please read this:

- jetbridge searches airline websites (aa.com, delta.com, united.com) automatically, which likely
  goes against their terms of use. You accept that risk by running it. It never logs in, so your
  frequent flyer accounts aren't involved, and it searches at low volume, but a site can still
  block its searches.
- It only checks while this PC is on, awake and logged in. To catch seats overnight, set Windows
  sleep to Never while plugged in.
- Checks are hours apart, so seats that open and close between them are missed. Don't rely on it
  as your only way of finding seats.
- It never books anything or moves your miles. You do that yourself.
- Only add email addresses of people who want these alerts.
"""


def _accept_risks(store: Store) -> bool:
    """Shown once. Nothing is set up or searched until it's accepted."""
    if store.get(_RISK_ACCEPTED, "0") == "1":
        return True
    typer.echo(_RISK_NOTICE)
    if not typer.confirm("Do you accept this and want to continue?", default=False):
        typer.echo("OK, nothing was set up.")
        return False
    store.set(_RISK_ACCEPTED, "1")
    typer.echo("")
    return True


def _email_ready(store: Store) -> bool:
    config = mail.load_config()
    if config is None or store.get(_EMAIL_FAILED, "0") == "1":
        return False
    return bool(mail.keyring.get_password(mail.KEYRING_SERVICE, config.from_address))


def _start_email(store: Store) -> None:
    """Set up email if needed. If the test fails, the next `start` asks for the details again."""
    if _email_ready(store):
        return
    typer.echo("First, let's set up the Gmail account that sends your alerts.\n")
    store.set(_EMAIL_FAILED, "1")
    try:
        notify_setup()
    except typer.Exit:
        typer.echo(
            "\nThe test email didn't send, so no flight was added. Fix the problem above, "
            "then run 'jetbridge start' again."
        )
        raise
    store.set(_EMAIL_FAILED, "0")
    typer.echo("Check that inbox now, and the spam folder if it isn't there.\n")


@app.command()
def start() -> None:
    """Set up jetbridge and add flights by answering a few questions. Start here."""
    store = service.open_store()
    first_run = not winsched.is_installed()
    try:
        if not _accept_risks(store):
            return
        _start_email(store)
        config = mail.load_config()
        owner = config.to_address if config else "you"
        added = wizard.add_watches(store, owner, date.today())
    except typer.Abort:
        typer.echo("\nStopped. Anything you confirmed is saved, and nothing else.")
        raise typer.Exit(code=1) from None

    if added and first_run:
        typer.echo("\nTurning on automatic checks...")
        try:
            winsched.install()
        except RuntimeError as exc:
            raise _fail(f"{exc}. Try 'jetbridge schedule install'") from exc
        typer.echo(f"Installed task '{winsched.TASK_NAME}'. It runs while you're logged on.\n")
        doctor()
        typer.echo(
            "\n'A clear answer from an airline' can say FAIL until the first check finishes, which "
            "is normal."
        )


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
