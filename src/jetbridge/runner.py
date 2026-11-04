"""One tick: run due checks, confirm openings, send alerts, report health."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from jetbridge import updater
from jetbridge.acquire.fetcher import Fetcher
from jetbridge.acquire.governor import Governor
from jetbridge.acquire.sites import SITES, get_site
from jetbridge.alerts import (
    AlertKind,
    DateState,
    Decision,
    confirm_opening,
    decide,
    format_when,
    query_for,
    render_availability,
)
from jetbridge.insights import Coverage, Seen, alert_note, coverage
from jetbridge.matcher import also_seen, evaluate
from jetbridge.models import AnswerKind, CheckResult, CheckStatus, Query, SearchAnswer, Watch
from jetbridge.notify import push
from jetbridge.notify.email import Sender, flush_outbox, owner_address
from jetbridge.scheduler import (
    MANUAL,
    NEIGHBOUR_DAYS,
    DateView,
    PlannedSearch,
    burst_is_due,
    group_queries,
    is_due,
    is_expired,
    next_visit_at,
    normal_gap_s,
    opens_on,
    plan_searches,
    search_starts,
    searchable_dates,
    spend_burst,
    start_burst,
    upcoming_dates,
)
from jetbridge.store import NewAlert, Store

TICK_BUDGET_S = 12 * 60
REQUEST_SPACING_S = (20.0, 60.0)
CONFIRM_DELAY_S = (60.0, 120.0)
DEFER_JITTER_S = (30.0, 300.0)
BLIND_AFTER_S = 24 * 3600
DRIFT_ALERT_AFTER = 2  # consecutive suspicious checks
NEVER_SEEN_AFTER = 6  # conclusive checks with the flight never listed
OFFLINE_NOTE_AFTER_S = 6 * 3600
DIGEST_HOUR = 8
COVERAGE_PERIOD_S = 24 * 3600


def watching_since(watch: Watch, day: date) -> float:
    """When jetbridge could first have searched for this date on this watch."""
    opened = datetime.combine(search_starts(day, watch.program), datetime.min.time()).timestamp()
    return max(watch.created_at, opened)


def never_seen_note(watch: Watch, domain: str, prefix: str = "") -> str:
    if watch.flight.is_any:
        return (
            f"{prefix}No nonstop flight has appeared in {domain}'s results for this route. The "
            f"airports may be wrong, or {domain} may not sell a nonstop here with these miles."
        )
    return (
        f"{prefix}{watch.flight.code} has never appeared in {domain}'s results for this route. The "
        f"flight number or route may be wrong, {domain} may not sell this flight with these miles, "
        "or it may just hide flights with no award space."
    )


def seen_history(store: Store, watch_id: int, day: date) -> list[Seen]:
    return [
        Seen(row["observed_at"], row["status"], row["miles"])
        for row in store.observations(watch_id, day)
    ]


def above_limit_miles(store: Store, watch: Watch, day: date) -> int | None:
    """The price of seats over this watch's limit, if the latest clear answer found some."""
    for row in reversed(store.observations(watch.id, day)):
        if row["status"] != CheckStatus.UNKNOWN.value:
            over = row["status"] == CheckStatus.NOT_OFFERED.value and row["miles"]
            return row["miles"] if over else None
    return None


def watch_coverage(store: Store, watch: Watch, now: float) -> Coverage | None:
    """The last 24 hours of the least-watched date, so a good date can't hide a bad one."""
    worst: Coverage | None = None
    today = datetime.fromtimestamp(now).date()
    for day in searchable_dates(watch, today):
        start = max(now - COVERAGE_PERIOD_S, watching_since(watch, day))
        found = coverage(
            store.clear_answers(watch.id, day, start),
            start,
            now,
            normal_gap_s(watch, day, today),
        )
        if found and (worst is None or found.share < worst.share):
            worst = found
    return worst


@dataclass
class CheckLine:
    watch_id: int
    label: str
    day: date
    result: CheckResult


@dataclass
class TickReport:
    checks: list[CheckLine] = field(default_factory=list)
    alerts_sent: int = 0
    stopped_early: str | None = None


class Runner:
    def __init__(
        self,
        store: Store,
        fetcher: Fetcher,
        sender: Sender,
        governor: Governor,
        *,
        clock: Callable[[], float],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        rng: random.Random | None = None,
        headlines: Mapping[AlertKind, str] | None = None,
    ):
        self.store, self.fetcher, self.sender, self.governor = store, fetcher, sender, governor
        self.clock, self.sleep = clock, sleep
        self.rng = rng or random.Random()
        self.headlines = headlines

    def _today(self) -> date:
        return datetime.fromtimestamp(self.clock()).date()

    async def tick(self, *, only_watch: int | None = None) -> TickReport:
        """Run due checks. With ``only_watch``, check that watch now, whatever its schedule."""
        report = TickReport()
        self._note_gap()
        self._finish_expired()

        now = self.clock()
        active = self.store.list_watches("active")
        if only_watch is not None:
            due = [w for w in active if w.id == only_watch]
        else:
            due = [w for w in active if is_due(w, now)]

        try:
            if due:
                await self._run_checks(due, report, paced=only_watch is None)
        finally:
            await self.fetcher.close()

        self._health_alerts()
        self._booking_reminders()
        self._digest()
        report.alerts_sent += flush_outbox(self.store, self.sender, self.clock())
        self.store.set("last_tick_at", str(self.clock()))
        return report

    # ---- checks ---------------------------------------------------------------------
    async def _run_checks(self, due: list[Watch], report: TickReport, paced: bool = True) -> None:
        today = self._today()
        started = self.clock()
        if paced:
            plan = plan_searches(due, today, started, self._view, self.governor.room_today)
        else:  # someone asked for this one, so it looks at everything the program can show
            plan = {
                query: PlannedSearch(tuple(watches), MANUAL)
                for query, watches in group_queries(due, today).items()
            }
        deadline = self.clock() + TICK_BUDGET_S
        retry_at: dict[int, float] = {}  # watches with dates that didn't get an answer
        stops: dict[str, SearchAnswer] = {}  # sites that may not be asked anything more
        last_site: str | None = None

        def stop_site(site: str, answer: SearchAnswer) -> None:
            stops[site] = SearchAnswer(
                AnswerKind.DEFERRED, detail=answer.detail, retry_at=answer.retry_at
            )
            note = f"{get_site(site).domain} {answer.kind.value}: {answer.detail}"
            report.stopped_early = (
                f"{report.stopped_early}; {note}" if report.stopped_early else note
            )

        for query, planned in plan.items():
            watches, reason = list(planned.watches), planned.reason
            site = query.program
            if site not in stops and self.clock() >= deadline:
                stops[site] = SearchAnswer(
                    AnswerKind.DEFERRED,
                    detail="time limit for this run",
                    retry_at=self.clock() + 900,
                )
            answer = stops.get(site)
            if answer is None:
                if last_site == site:
                    await self.sleep(self.rng.uniform(*REQUEST_SPACING_S))
                last_site = site
                answer = await self.fetcher.search(query, deadline)
                if answer.kind in (AnswerKind.BLOCKED, AnswerKind.DEFERRED):
                    stop_site(site, answer)

            if answer.kind in (AnswerKind.BLOCKED, AnswerKind.DEFERRED) and answer.retry_at:
                for watch in watches:
                    retry_at[watch.id] = answer.retry_at

            confirm = self._apply(query, watches, answer, report, reason)
            self._note_parser_health(query, watches, answer)
            if confirm:
                await self.sleep(self.rng.uniform(*CONFIRM_DELAY_S))
                recheck = await self.fetcher.search(query, deadline)
                if recheck.kind in (AnswerKind.BLOCKED, AnswerKind.DEFERRED):
                    stop_site(site, recheck)
                self._apply_confirmation(query, confirm, recheck, report, reason)
            report.alerts_sent += flush_outbox(self.store, self.sender, self.clock())

        now = self.clock()
        for watch in due:
            if watch.id in retry_at:
                at = retry_at[watch.id] + self.rng.uniform(*DEFER_JITTER_S)
            else:
                bursts = [
                    burst
                    for day in searchable_dates(watch, today)
                    if (burst := self.store.get_burst(watch.id, day)) is not None
                ]
                at = next_visit_at(watch, now, today, bursts, self.rng)
            self.store.set_next_check(watch.id, at)

    def _view(self, watch: Watch, day: date) -> DateView:
        state = self.store.date_state(watch.id, day)
        return DateView(
            state.last_conclusive_at, state.last_check_at, self.store.get_burst(watch.id, day)
        )

    def _note_movement(self, watch: Watch, day: date, reason: str) -> None:
        """Seats or prices moved, so the next checks on this date come sooner, within the budget."""
        current = self.store.get_burst(watch.id, day)
        self.store.set_burst(watch.id, day, start_burst(current, reason, self.clock(), self.rng))

    def _spend_burst(self, watch: Watch, day: date) -> None:
        current = self.store.get_burst(watch.id, day)
        if current is not None and burst_is_due(current, self.clock()):
            self.store.set_burst(watch.id, day, spend_burst(current, self.clock(), self.rng))

    def _apply(
        self,
        query: Query,
        watches: list[Watch],
        answer: SearchAnswer,
        report: TickReport,
        reason: str | None = None,
    ) -> list[tuple[Watch, CheckResult, DateState]]:
        """Record results. Returns the watches whose opening needs a live re-check."""
        pending = []
        now = self.clock()
        for watch in watches:
            result = evaluate(watch, query.date, answer)
            report.checks.append(CheckLine(watch.id, watch.label, query.date, result))
            prev = self.store.date_state(watch.id, query.date)
            decision = decide(prev, result, now)
            self._spend_burst(watch, query.date)
            if answer.kind is AnswerKind.ANSWER:
                before = self.store.get_also_seen(watch.id, query.date)
                known = {o["flight"] for o in before["flights"]} if before else set()
                flights = [
                    {
                        "flight": other.flight,
                        "departs": other.departs.isoformat(),
                        "miles": other.miles,
                    }
                    for other in also_seen(watch, query, answer)
                ]
                self.store.set_also_seen(watch.id, query.date, flights, now)
                if prev.last_check_at is not None and {o["flight"] for o in flights} - known:
                    self._note_movement(watch, query.date, "another flight opened on this route")
            if AlertKind.SEATS_GONE in decision.alerts:
                self._note_movement(watch, query.date, "seats just went")
            elif (
                result.status is CheckStatus.AVAILABLE
                and prev.miles
                and result.miles
                and prev.miles != result.miles
            ):
                self._note_movement(watch, query.date, "the price just changed")
            self._commit(watch, query.date, result, prev, decision, now, reason=reason)
            if decision.needs_confirmation:
                pending.append((watch, result, decision.state))
        return pending

    def _apply_confirmation(
        self,
        query: Query,
        pending: list[tuple[Watch, CheckResult, DateState]],
        answer: SearchAnswer,
        report: TickReport,
        reason: str | None = None,
    ) -> None:
        now = self.clock()
        for watch, first, state in pending:
            recheck = evaluate(watch, query.date, answer)
            report.checks.append(CheckLine(watch.id, watch.label, query.date, recheck))
            decision = confirm_opening(state, first, recheck, now)
            shown = first if decision.alerts == (AlertKind.SEATS_OPEN_UNCONFIRMED,) else recheck
            self._commit(
                watch, query.date, recheck, state, decision, now, shown=shown, reason=reason
            )
            if recheck.status is CheckStatus.AVAILABLE:
                for other in watch.dates:
                    if other != query.date and abs((other - query.date).days) <= NEIGHBOUR_DAYS:
                        self._note_movement(watch, other, "a neighbouring date opened")
            elif recheck.status.conclusive:
                self._note_movement(watch, query.date, "seats appeared and went quickly")

    def _commit(
        self,
        watch: Watch,
        day: date,
        result: CheckResult,
        prev: DateState,
        decision: Decision,
        now: float,
        shown: CheckResult | None = None,
        reason: str | None = None,
    ) -> None:
        url = get_site(watch.program).search_url(query_for(watch, day))
        alerts = []
        for kind in decision.alerts:
            note = None
            if kind in (AlertKind.SEATS_OPEN, AlertKind.SEATS_OPEN_UNCONFIRMED):
                note = alert_note(seen_history(self.store, watch.id, day), now)
            subject, body = render_availability(
                kind, watch, day, shown or result, prev, now, url, self.headlines, note
            )
            # One email per person, so recipients never see each other's addresses.
            for recipient in watch.recipients or (None,):
                alerts.append(NewAlert(kind.value, subject, body, recipient=recipient))
            if push.wanted_for(kind):
                alerts.append(push.alert_for(kind, subject, url))
        self.store.commit_check(watch.id, day, result, decision.state, alerts, now, reason)

    # ---- health ---------------------------------------------------------------------
    def _note_parser_health(self, query: Query, watches: list[Watch], answer: SearchAnswer) -> None:
        site = query.program
        results = [evaluate(w, query.date, answer) for w in watches]
        suspect = next((r for r in results if r.suspect_parser), None)
        if suspect is not None:
            count = int(self.store.get(f"drift_count:{site}", "0") or 0) + 1
            self.store.set(f"drift_count:{site}", str(count))
            if not self.store.get(f"drift_since:{site}"):
                self.store.set(f"drift_since:{site}", str(self.clock()))
            self.store.set(f"drift_detail:{site}", suspect.detail or "unreadable result")
        elif answer.kind is AnswerKind.ANSWER:
            self.store.delete(f"drift_count:{site}")
            self.store.delete(f"drift_since:{site}")

    def _health_alerts(self) -> None:
        now = self.clock()
        today = self._today()
        for watch in self.store.list_watches("active"):
            days = searchable_dates(watch, today)
            if not days:
                continue
            last_good = max(
                self.store.date_state(watch.id, d).last_conclusive_at or watching_since(watch, d)
                for d in days
            )
            # A slow watch is normally quiet for longer, and that alone isn't a problem.
            longest = max(normal_gap_s(watch, d, today) for d in days)
            if now - last_good >= max(BLIND_AFTER_S, 2 * longest):
                hours = (now - last_good) / 3600
                self.store.enqueue(
                    NewAlert(
                        "blind",
                        f"jetbridge can't see {watch.name}: no clear answer in {hours:.0f} hours",
                        f"{watch.label} has had no conclusive check since "
                        f"{format_when(last_good)}.\n\nUntil this clears, silence does NOT "
                        "mean there are no seats. Run 'jetbridge doctor', or ask Claude, to see "
                        "why.",
                        dedupe_key=f"blind:{watch.id}:{int(last_good)}",
                    ),
                    now,
                )

        for site in SITES.values():
            count = int(self.store.get(f"drift_count:{site.id}", "0") or 0)
            since = self.store.get(f"drift_since:{site.id}")
            if count >= DRIFT_ALERT_AFTER and since:
                detail = self.store.get(f"drift_detail:{site.id}", "") or ""
                self.store.enqueue(
                    NewAlert(
                        "drift",
                        f"jetbridge: {site.domain}'s page format may have changed",
                        "Recent responses could not be read, so checks are coming back as "
                        f"'unknown' rather than 'no seats'.\n\nLatest detail: {detail}\n\n"
                        "The raw responses are saved for repairing the parser. If a fix is out, "
                        "'jetbridge update' installs it.",
                        dedupe_key=f"drift:{site.id}:{int(float(since))}",
                    ),
                    now,
                )

    def _booking_reminders(self) -> None:
        """Say the day before a date should become bookable. This doesn't take any searches."""
        today = self._today()
        for watch in self.store.list_watches("active"):
            for day in upcoming_dates(watch, today):
                opens = opens_on(day, watch.program)
                if not opens - timedelta(days=1) <= today <= opens:
                    continue
                when = "today" if today == opens else "tomorrow"
                site = get_site(watch.program)
                subject = f"{day:%b %d, %Y} should open {when} on {site.label}: {watch.name}"
                body = (
                    f"{watch.label} on {day:%b %d, %Y} should become bookable {when} on "
                    f"{site.label}. This goes by the program's usual booking window, which can be "
                    "a day or so out, and seats aren't guaranteed.\n\n"
                    f"Check and book:\n{site.search_url(query_for(watch, day))}\n\n"
                    "jetbridge will check it again soon after it opens."
                )
                for recipient in watch.recipients or (None,):
                    self.store.enqueue(
                        NewAlert(
                            "booking_opens",
                            subject,
                            body,
                            dedupe_key=f"booking_opens:{watch.id}:{day}:{recipient}",
                            recipient=recipient,
                        ),
                        self.clock(),
                    )

    def _note_gap(self) -> None:
        now = self.clock()
        last = self.store.get("last_tick_at")
        if last and now - float(last) >= OFFLINE_NOTE_AFTER_S:
            longest = max(float(self.store.get("longest_gap_s", "0") or 0), now - float(last))
            self.store.set("longest_gap_s", str(longest))

    def _finish_expired(self) -> None:
        today = self._today()
        for watch in self.store.list_watches("active"):
            if is_expired(watch, today):
                self.store.set_watch_status(watch.id, "finished")

    def _digest(self) -> None:
        if self.store.get("digest_enabled", "1") != "1":
            return
        watches = self.store.list_watches("active")
        local = datetime.fromtimestamp(self.clock())
        key = local.date().isoformat()
        if not watches or local.hour < DIGEST_HOUR or self.store.get("last_digest") == key:
            return
        subject, body = self.build_digest(watches)
        self.store.enqueue(
            NewAlert("digest", subject, body, dedupe_key=f"digest:{key}"), self.clock()
        )
        owner = (owner_address() or "").casefold()
        people = {a for w in watches for a in w.recipients if a.casefold() != owner}
        for address in sorted(people):
            theirs = [w for w in watches if address in w.recipients]
            subject, body = self.build_digest(theirs, for_owner=False)
            self.store.enqueue(
                NewAlert(
                    "digest", subject, body, dedupe_key=f"digest:{key}:{address}", recipient=address
                ),
                self.clock(),
            )
        self.store.set("last_digest", key)
        self.store.delete("longest_gap_s")

    def build_digest(self, watches: list[Watch], for_owner: bool = True) -> tuple[str, str]:
        """The owner's copy also has who's on each list and how the watcher is doing. Someone
        else's copy covers only their own watches and never names the other people on them."""
        now = self.clock()
        today = self._today()
        day_ago = now - 86400
        open_count = 0
        lines: list[str] = []
        for watch in watches:
            good = sum(
                self.store.count_recent(watch.id, status, day_ago)
                for status in CheckStatus
                if status.conclusive
            )
            unknown = self.store.count_recent(watch.id, CheckStatus.UNKNOWN, day_ago)
            domain = get_site(watch.program).domain
            title = f"{watch.label} via {get_site(watch.program).label}"
            lines.append(f"{title} (watch {watch.id})" if for_owner else title)
            if for_owner:
                lines.append(f"  Seat alerts go to: {', '.join(watch.recipients) or 'you'}")
            lines.append(f"  Last 24 hours: {good} clear answers, {unknown} inconclusive")
            watched = watch_coverage(self.store, watch, now)
            if watched is not None:
                lines.append(f"  Proof of watching: {watched.text}")
            all_never_seen = True
            searchable = searchable_dates(watch, today)
            for day in upcoming_dates(watch, today):
                if day not in searchable:
                    starts = search_starts(day, watch.program)
                    lines.append(f"  {day:%b %d}: searching starts {starts:%b %d, %Y}")
                    all_never_seen = False
                    continue
                state = self.store.date_state(watch.id, day)
                if state.status is CheckStatus.AVAILABLE:
                    open_count += 1
                text = state.status.value.replace("_", " ") if state.status else "not checked yet"
                price = f", {state.miles:,} miles" if state.miles else ""
                over = above_limit_miles(self.store, watch, day)
                if over and watch.booked_miles:
                    paid = f"{watch.booked_miles:,}"
                    price = f", seats at {over:,} miles, not below the {paid} paid"
                elif over and watch.max_miles:
                    whose = "your" if for_owner else "the"
                    price = f", seats at {over:,} miles, above {whose} {watch.max_miles:,} limit"
                lines.append(f"  {day:%b %d}: {text}{price}")
                others = self.store.get_also_seen(watch.id, day)
                if others and state.status is not CheckStatus.AVAILABLE:
                    names = ", ".join(
                        f"{o['flight']} ({o['miles']:,} miles)" if o["miles"] else o["flight"]
                        for o in others["flights"]
                    )
                    lines.append(
                        f"    Also seen with {watch.cabin.value} seats on this route and date, "
                        f"as of {format_when(others['at'])}: {names}. jetbridge isn't watching "
                        "these and hasn't re-checked them, so they may be gone, and the list may "
                        "be incomplete."
                    )
                if state.ever_listed or state.conclusive_count < NEVER_SEEN_AFTER:
                    all_never_seen = False
            if all_never_seen and for_owner:
                lines.append(never_seen_note(watch, domain, "  Note: "))
            lines.append("")

        status = f"{open_count} date(s) OPEN" if open_count else "no seats yet"
        subject = f"jetbridge daily: {status}, still watching"
        if not for_owner:
            lines.append(
                "If this daily email stops arriving, the watcher may have stopped, so check with "
                "the person who added you."
            )
            return subject, "\n".join(lines)

        gap = float(self.store.get("longest_gap_s", "0") or 0)
        if gap:
            lines.append(
                f"This computer was off or asleep for up to {gap / 3600:.0f} hours since the "
                "last digest, so no checks ran during that time."
            )
        for site in SITES.values():
            until = self.governor.cooldown_until(site.id)
            if until > now:
                lines.append(f"{site.domain} is resting after a block until {format_when(until)}.")
        newer = updater.newer_version(self.store)
        if newer:
            lines.append(
                f"jetbridge {newer} is available, which may include fixes for airline sites. Run "
                "'jetbridge update', or ask Claude to update it."
            )
        lines.append("If this daily email stops arriving, the watcher isn't running.")
        return subject, "\n".join(lines)
