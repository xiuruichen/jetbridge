"""Command line interface."""

from __future__ import annotations

import importlib.metadata
import re
import time
from datetime import date, datetime

import typer
import typer.core

from jetbridge import autorun, claude_desktop, headlines, platforms, power, service, updater, wizard
from jetbridge import doctor as doctor_checks
from jetbridge import draft as drafts
from jetbridge import insights as record
from jetbridge import report as problem_report
from jetbridge import uninstall as removal
from jetbridge.acquire.governor import DAILY_LIMIT, Governor
from jetbridge.acquire.sites import SITES
from jetbridge.alerts import DEFAULT_HEADLINES, AlertKind, format_when
from jetbridge.models import CheckStatus, Watch
from jetbridge.notify import email as mail
from jetbridge.notify import push
from jetbridge.paths import home, raw_dir
from jetbridge.runner import seen_history
from jetbridge.store import Store


class _StartFirst(typer.core.TyperGroup):
    """List commands in the order someone needs them, not the order they're written."""

    ORDER = ("start", "status", "doctor", "watch", "check", "history", "notify", "claude")

    def list_commands(self, ctx) -> list[str]:
        names = super().list_commands(ctx)
        return [
            *[name for name in self.ORDER if name in names],
            *[name for name in names if name not in self.ORDER],
        ]


app = typer.Typer(
    cls=_StartFirst,
    no_args_is_help=True,
    add_completion=False,
    help="Watch award seats on a flight, and get an email when they open.",
)
watch_app = typer.Typer(no_args_is_help=True, help="Add and manage watches.")
notify_app = typer.Typer(no_args_is_help=True, help="Set up and test email alerts.")
schedule_app = typer.Typer(no_args_is_help=True, help="Run checks automatically on this computer.")
app.add_typer(watch_app, name="watch")
app.add_typer(notify_app, name="notify")
claude_app = typer.Typer(no_args_is_help=True, help="Use jetbridge from the Claude Desktop app.")
app.add_typer(schedule_app, name="schedule")
app.add_typer(claude_app, name="claude")


def _show_version(value: bool) -> None:
    if value:
        typer.echo(f"jetbridge {importlib.metadata.version('jetbridge')}")
        raise typer.Exit


@app.callback()
def _main(
    version: bool = typer.Option(
        False, "--version", callback=_show_version, is_eager=True, help="Show the version and exit."
    ),
) -> None:
    pass


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
        f"[{watch.id}] {watch.label} via {SITES[watch.program].label}, {watch.pax} pax, "
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
    flight: str = typer.Argument(..., help="Flight number, e.g. AA100, or 'any' for any nonstop"),
    origin: str = typer.Option(..., "--from", help="Departure airport of this flight"),
    destination: str = typer.Option(..., "--to", help="Arrival airport of this flight"),
    dates: str = typer.Option(..., help="2027-06-14..2027-06-16 or 2027-06-14,2027-06-16"),
    cabin: str = typer.Option("business", help="economy, premium, business or first"),
    program: str = typer.Option("american", help="Miles to search: american, delta or united"),
    pax: int = typer.Option(1, help="Party size; searched at this real size"),
    checks_per_day: int = typer.Option(
        6, help="Checks per day, per date. Lower far out, higher in the last two weeks"
    ),
    max_miles: int | None = typer.Option(None, help="Only alert at or below this price"),
    notify: str | None = typer.Option(
        None, help="Who gets seat alerts: a@x.com,b@y.com or 'me'. Prompts if left out"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking, for use in scripts"),
) -> None:
    """Start watching a flight, after showing what will be saved."""
    store = service.open_store()
    try:
        service.parse_dates(dates)  # catch a bad date before asking anything else
        if notify is None:
            notify = _ask_recipients()
        valid = service.validate_watch(
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
        draft = drafts.WatchDraft(
            origin=valid.origin,
            destination=valid.destination,
            dates=dates,
            flight=valid.flight.code,
            cabin=valid.cabin,
            program=program,
            pax=pax,
            checks_per_day=checks_per_day,
            max_miles=max_miles,
            recipients=notify,
        )
        if not yes:
            owner = mail.load_config()
            typer.echo("\nHere's what I'll watch:")
            typer.echo(drafts.review_text(draft, owner.to_address if owner else "you"))
            try:
                if not typer.confirm("\nStart watching this flight?", default=False):
                    typer.echo("OK, nothing was saved.")
                    return
            except typer.Abort as exc:
                raise _fail("nothing was saved. Add --yes to save without asking") from exc
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
    if autorun.is_installed():
        typer.echo("The first check runs within about 15 minutes.")
    else:
        typer.echo("Automatic checks are off. Turn them on with: jetbridge schedule install")
    typer.echo(f"To check it now, run: jetbridge check {watch.id}")


@watch_app.command("edit")
def watch_edit(
    watch_id: int,
    dates: str | None = typer.Option(None, help="New dates, which replace the old ones"),
    checks_per_day: int | None = typer.Option(None, help="Checks per day, per date"),
    max_miles: int | None = typer.Option(None, help="New price limit per person"),
    no_limit: bool = typer.Option(False, "--no-limit", help="Remove the price limit"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Save without asking, for use in scripts"),
) -> None:
    """Change a watch's dates, price limit or pace, keeping its history."""
    store = service.open_store()
    watch = _require(store, watch_id)
    if max_miles is not None and no_limit:
        raise _fail("choose a new price limit or --no-limit, not both")
    new_dates = dates or ",".join(day.isoformat() for day in watch.dates)
    new_pace = checks_per_day if checks_per_day is not None else watch.checks_per_day
    new_limit = None if no_limit else (max_miles if max_miles is not None else watch.max_miles)
    try:
        draft = drafts.from_watch(watch, new_dates, new_pace, new_limit)
        drafts.validate(draft)
        if not yes:
            owner = mail.load_config()
            typer.echo("\nHere's how it will look. Its history is kept:")
            typer.echo(
                drafts.review_text(draft, owner.to_address if owner else "you", editing=True)
            )
            try:
                if not typer.confirm("\nSave this change?", default=False):
                    typer.echo("OK, nothing was changed.")
                    return
            except typer.Abort as exc:
                raise _fail("nothing was changed. Add --yes to save without asking") from exc
        updated = service.change_watch(
            store, watch, dates=new_dates, checks_per_day=new_pace, max_miles=new_limit
        )
    except ValueError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(_describe(updated))


@watch_app.command("priority")
def watch_priority(watch_id: int, level: str = typer.Argument(..., help="high or normal")) -> None:
    """Make a watch go first and get checked more often, within the same daily limits."""
    if level not in ("high", "normal"):
        raise _fail("say high or normal")
    store = service.open_store()
    _require(store, watch_id)
    store.set_high_priority(watch_id, level == "high")
    typer.echo(f"Watch {watch_id} is now {level} priority.")


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
            watch, welcomed = service.set_recipients(
                store, watch, service.parse_recipients(addresses)
            )
        except ValueError as exc:
            raise _fail(str(exc)) from exc
        for address in welcomed:
            typer.echo(f"{address} will get one short note saying they were added.")
    typer.echo(f"Seat alerts for watch {watch_id} go to: {_recipients_text(watch)}")


@watch_app.command("list")
def watch_list() -> None:
    """List all watches."""
    watches = service.open_store().list_watches()
    if not watches:
        typer.echo("No watches yet. Add one with: jetbridge start")
    for watch in watches:
        typer.echo(_describe(watch))


@watch_app.command("pause")
def watch_pause(watch_id: int) -> None:
    """Stop checking a watch and keep its history."""
    store = service.open_store()
    _require(store, watch_id)
    store.set_watch_status(watch_id, "paused")
    typer.echo(f"Paused watch {watch_id}.")


@watch_app.command("resume")
def watch_resume(watch_id: int) -> None:
    """Start checking a paused watch again."""
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


@app.command(hidden=True)
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
    """Check one watch now, within the usual limits on searching."""
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
                f"{used}/{budget.max_requests}, last 24 hours: "
                f"{governor.used_today(site.id)}/{DAILY_LIMIT}"
            )
        if resting:
            typer.echo(f"{site.domain} is RESTING after a block until {format_when(until)}.")
    last = store.get("last_tick_at")
    typer.echo(f"Last tick: {format_when(float(last)) if last else 'never'}")
    pending = [item for item in store.pending_alerts() if item.recipient != push.PUSH_RECIPIENT]
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
def insights(watch_id: int) -> None:
    """What jetbridge has seen for a watch so far: openings, how long they lasted, and prices."""
    store = service.open_store()
    watch = _require(store, watch_id)
    typer.echo(_describe(watch))
    now = time.time()
    for day in watch.dates:
        history = seen_history(store, watch_id, day)
        typer.echo(f"  {day:%b %d, %Y}: {record.clear_answers(history)} clear answers")
        found = record.openings(history)
        if not found:
            typer.echo("    No seats seen.")
        for opening in found:
            price = f", {opening.lowest_miles:,} miles" if opening.lowest_miles else ""
            typer.echo(
                f"    Seen {format_when(opening.first_seen)}, {opening.lasted(now)}{price}, "
                f"{(day - datetime.fromtimestamp(opening.first_seen).date()).days} days ahead"
            )
    typer.echo("This is only what jetbridge saw at its checks. It doesn't predict future seats.")


@app.command()
def doctor() -> None:
    """Check that everything needed to watch and alert is working."""
    store = service.open_store()
    for check_result in doctor_checks.checks(store):
        mark = " OK " if check_result.ok else "FAIL"
        fix = "" if check_result.ok else f" -> {check_result.fix}"
        typer.echo(f"[{mark}] {check_result.label}{fix}")
    good = doctor_checks.last_clear_answer(store)
    if good is not None:
        typer.echo(f"Last clear answer: {good}")
    _print_health(store)
    if platforms.IS_MAC:
        typer.echo(platforms.MAC_NOTICE)
    typer.echo(f"Claude Desktop (optional): {claude_desktop.status().value}")
    newer = updater.newer_version(store)
    if newer:
        typer.echo(f"jetbridge {newer} is available. Run: jetbridge update")
    typer.echo(f"State folder: {home()}")
    typer.echo(f"Saved failed responses: {len(list(raw_dir().glob('*.html')))}")


# ---- notify ---------------------------------------------------------------------------
def _ask_address(label: str, default: str | None = None) -> str:
    while True:
        text = typer.prompt(label, default=default).strip()
        if re.fullmatch(r"[^@\s,;]+@[^@\s,;]+\.[^@\s,;]+", text):
            return text
        typer.echo("  That doesn't look like an email address. It should look like you@gmail.com.")


@notify_app.command("setup")
def notify_setup() -> None:
    """Save your Gmail address and app password. The password goes in Credential Manager."""
    typer.echo("Create an app password at https://myaccount.google.com/apppasswords")
    typer.echo("(this needs 2-Step Verification on the Google account).")
    sender = _ask_address("Send from (Gmail address)")
    to = _ask_address("Send alerts to", default=sender)
    while not (password := typer.prompt("Gmail app password (hidden)", hide_input=True).strip()):
        typer.echo("  That was empty. Paste the 16-letter app password from Google.")
    mail.save_config(mail.EmailConfig(sender, to), password)
    typer.echo("Saved. Sending a test email...")
    notify_test(to=None)


@notify_app.command("setup-window", hidden=True)
def notify_setup_window() -> None:
    """The window Claude opens, so a password is never typed into a chat."""
    platforms.set_window_title("jetbridge email setup")
    typer.echo("jetbridge email setup. What you type here stays on this computer.\n")
    try:
        notify_setup()
        typer.echo("\nAll done. You can go back to Claude and tell it you've finished.")
    except typer.Exit:
        typer.echo("\nThat didn't work. You can close this and ask Claude to open it again.")
    except typer.Abort:
        typer.echo("\nStopped. Nothing else was changed.")
    typer.prompt("Press Enter to close this window", default="", show_default=False)


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


_PHONE_NOTICE = """Phone alerts tell you the moment seats open, as well as emailing you.

They go through ntfy.sh, a free service with no account. It sees the alert's text and booking link,
and anyone who knows your private channel name could read them. jetbridge makes a long random name
and keeps it on this computer."""


def _phone_steps(name: str) -> None:
    typer.echo(f"""
1. Install the free ntfy app on your phone, from https://ntfy.sh
2. In the app, press + and subscribe to this channel, exactly as written:

   {name}

Keep that name private.""")


@notify_app.command("push")
def notify_push(state: str = typer.Argument(..., help="on, off or test")) -> None:
    """Turn phone alerts through ntfy on or off, or send a test to your phone."""
    if state == "on":
        typer.echo(_PHONE_NOTICE)
        if not typer.confirm("\nTurn on phone alerts?", default=False):
            typer.echo("OK, nothing changed.")
            return
        _phone_steps(push.turn_on())
        typer.echo("\nThen run: jetbridge notify push test")
    elif state == "off":
        push.turn_off()
        typer.echo("Phone alerts are off. Emails carry on, and you can delete the app.")
    elif state == "test":
        try:
            push.send_test()
        except (OSError, LookupError) as exc:
            raise _fail(f"couldn't send it: {exc}") from exc
        typer.echo("Sent. If nothing arrives, check the channel name in the app.")
    else:
        raise _fail("say on, off or test")


@notify_app.command("push-window", hidden=True)
def notify_push_window() -> None:
    """The private window Claude opens, so the channel name never goes in a chat."""
    typer.echo(_PHONE_NOTICE)
    if not typer.confirm("\nTurn on phone alerts?", default=False):
        typer.echo("OK, nothing changed.")
    else:
        _phone_steps(push.turn_on())
        typer.prompt("\nPress Enter once you've subscribed", default="", show_default=False)
        try:
            push.send_test()
            typer.echo("Sent a test to your phone. If nothing arrives, check the channel name.")
        except OSError as exc:
            typer.echo(f"Couldn't send a test: {exc}")
    typer.prompt("\nPress Enter to close this window", default="", show_default=False)


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


def _accept_risks(store: Store) -> bool:
    """Shown once. Nothing is set up or searched until it's accepted."""
    if store.get(service.RISK_ACCEPTED, "0") == "1":
        return True
    typer.echo(service.RISK_NOTICE)
    if not typer.confirm("Do you accept this and want to continue?", default=False):
        typer.echo("OK, nothing was set up.")
        return False
    store.set(service.RISK_ACCEPTED, "1")
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
    first_run = not autorun.is_installed()
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
            autorun.install()
        except RuntimeError as exc:
            raise _fail(f"{exc}. Try 'jetbridge schedule install'") from exc
        typer.echo(f"Installed task '{autorun.task_name()}'. It runs while you're logged on.\n")
        doctor()
        typer.echo(
            "\n'A clear answer from an airline' can say FAIL until the first check finishes, which "
            "is normal."
        )
    if added:
        _first_checks(store, added)


def _first_checks(store: Store, watches: list[Watch]) -> None:
    """Check each new watch now, so a wrong flight or route shows up today rather than days on."""
    typer.echo("\nChecking now that the site can see your flight. This takes a minute or two...")
    for watch in watches:
        try:
            report = service.live_tick(store, only_watch=watch.id)
        except service.TickBusy:
            typer.echo(f"{watch.name}: a check is already running, so this one runs soon.")
            continue
        except Exception as exc:  # the watch is saved, and scheduled checks will still try
            typer.echo(f"{watch.name}: couldn't check it just now ({exc}). It'll try again.")
            continue
        latest = {line.day: line.result for line in report.checks}
        if not latest:
            typer.echo(f"{watch.name}: its dates can't be searched yet.")
        for day, result in latest.items():
            means = service.first_check_means(watch, result)
            typer.echo(f"{watch.name} {day:%b %d}: {means}")


# ---- claude ---------------------------------------------------------------------------
_DOWNLOAD_CLAUDE = "https://claude.com/download"


def _ask_to_connect() -> bool:
    """The installer's question. Says why when there's nothing to ask, and never fails."""
    state = claude_desktop.status()
    if state is claude_desktop.State.NOT_INSTALLED:
        typer.echo(
            "Claude Desktop isn't installed, which is fine. If you install it later, run "
            "'jetbridge claude connect' to manage your watches by chatting."
        )
        return False
    if state is claude_desktop.State.CONNECTED:
        typer.echo("Claude Desktop is already connected to jetbridge.")
        return False
    try:
        return typer.confirm(
            "Claude Desktop is installed. Connect jetbridge to it, so you can manage your watches "
            "by chatting?",
            default=True,
        )
    except typer.Abort:
        return False


@claude_app.command("connect")
def claude_connect(
    ask: bool = typer.Option(False, "--ask", help="Ask first, and don't fail if it can't be done."),
) -> None:
    """Add jetbridge to Claude Desktop's config. Your other servers and settings are kept."""
    if ask and not _ask_to_connect():
        return
    try:
        changed = claude_desktop.connect()
    except claude_desktop.ConfigProblem as exc:
        message = f"{exc}."
        if claude_desktop.status() is claude_desktop.State.NOT_INSTALLED:
            message += f" Get it from {_DOWNLOAD_CLAUDE}, open it once, then run this again."
        if ask:
            typer.echo(f"Couldn't connect Claude Desktop: {message}")
            return
        raise _fail(message) from exc
    if not changed:
        typer.echo("Claude Desktop is already connected to jetbridge.")
        return
    typer.echo(
        "Connected. Quit Claude Desktop fully, including from the system tray, and reopen it. Then "
        'try asking: "Is my jetbridge watcher healthy?"'
    )


@claude_app.command("disconnect")
def claude_disconnect() -> None:
    """Remove jetbridge from Claude Desktop's config. Your watches and alerts are unaffected."""
    try:
        changed = claude_desktop.disconnect()
    except claude_desktop.ConfigProblem as exc:
        raise _fail(str(exc)) from exc
    typer.echo(
        "Removed. Restart Claude Desktop for it to take effect."
        if changed
        else "Claude Desktop wasn't connected to jetbridge."
    )


@app.command("uninstall")
def uninstall_command(
    yes: bool = typer.Option(False, "--yes", "-y", help="Don't ask first"),
) -> None:
    """Remove jetbridge's checks, watches, settings and saved password from this computer."""
    typer.echo(
        "This stops all checks and deletes your watches, their history, your email settings and "
        f"your saved password, including everything in {home()}. It can't be undone."
    )
    if not yes and not typer.confirm("Remove jetbridge?", default=False):
        typer.echo("OK, nothing was removed.")
        return
    for line in removal.remove_everything():
        typer.echo(line)


@claude_app.command("status")
def claude_status() -> None:
    """Show whether Claude Desktop is connected to jetbridge."""
    typer.echo(f"Claude Desktop: {claude_desktop.status().value}")


@app.command()
def update() -> None:
    """Install the newest jetbridge, including any fixes for airline sites."""
    try:
        for line in updater.install():
            typer.echo(line)
    except updater.UpdateProblem as exc:
        raise _fail(str(exc)) from exc


@app.command("report")
def report_command() -> None:
    """Prepare a problem report to read and post, with nothing personal in it."""
    built = problem_report.build(service.open_store())
    typer.echo(built.body)
    typer.echo("\nNothing has been sent. Read it, and if you're happy to share it, open this link:")
    typer.echo(built.url)


@app.command("keep-awake")
def keep_awake(state: str = typer.Argument(..., help="on or off")) -> None:
    """Keep this computer awake while plugged in, so checks keep running. Off puts it back."""
    if state not in ("on", "off"):
        raise _fail("say on or off")
    store = service.open_store()
    try:
        if state == "on":
            power.keep_awake(store)
            typer.echo(
                "This computer now stays awake while plugged in. It can still sleep on battery."
            )
        elif power.restore(store):
            typer.echo("Your computer's sleep settings are back as they were.")
        else:
            typer.echo("jetbridge hasn't changed your sleep settings, so there's nothing to undo.")
    except OSError as exc:
        raise _fail(str(exc)) from exc


# ---- schedule -------------------------------------------------------------------------
@schedule_app.command("install")
def schedule_install() -> None:
    """Create the background task that runs a tick every 15 minutes."""
    try:
        autorun.install()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.echo(f"Installed task '{autorun.task_name()}'. It runs while you're logged on.")


@schedule_app.command("remove")
def schedule_remove() -> None:
    """Remove the scheduled task. Watches are kept."""
    try:
        autorun.remove()
    except RuntimeError as exc:
        raise _fail(str(exc)) from exc
    typer.echo("Removed the scheduled task.")
