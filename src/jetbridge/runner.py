"""One tick: run due checks, confirm openings, send alerts, report health."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime

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
from jetbridge.matcher import evaluate
from jetbridge.models import AnswerKind, CheckResult, CheckStatus, Query, SearchAnswer, Watch
from jetbridge.notify.email import Sender, flush_outbox
from jetbridge.scheduler import (
    group_queries,
    is_due,
    is_expired,
    longest_gap_s,
    next_check_at,
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
                await self._run_checks(due, report)
        finally:
            await self.fetcher.close()

        self._health_alerts()
        self._digest()
        report.alerts_sent += flush_outbox(self.store, self.sender, self.clock())
        self.store.set("last_tick_at", str(self.clock()))
        return report

    # ---- checks ---------------------------------------------------------------------
    async def _run_checks(self, due: list[Watch], report: TickReport) -> None:
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

        for query, watches in group_queries(due, self._today()).items():
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

            confirm = self._apply(query, watches, answer, report)
            self._note_parser_health(query, watches, answer)
            if confirm:
                await self.sleep(self.rng.uniform(*CONFIRM_DELAY_S))
                recheck = await self.fetcher.search(query, deadline)
                if recheck.kind in (AnswerKind.BLOCKED, AnswerKind.DEFERRED):
                    stop_site(site, recheck)
                self._apply_confirmation(query, confirm, recheck, report)
            report.alerts_sent += flush_outbox(self.store, self.sender, self.clock())

        now = self.clock()
        for watch in due:
            if watch.id in retry_at:
                at = retry_at[watch.id] + self.rng.uniform(*DEFER_JITTER_S)
            else:
                at = next_check_at(now, watch.checks_per_day, self.rng)
            self.store.set_next_check(watch.id, at)

    def _apply(
        self, query: Query, watches: list[Watch], answer: SearchAnswer, report: TickReport
    ) -> list[tuple[Watch, CheckResult, DateState]]:
        """Record results. Returns the watches whose opening needs a live re-check."""
        pending = []
        now = self.clock()
        for watch in watches:
            result = evaluate(watch, query.date, answer)
            report.checks.append(CheckLine(watch.id, watch.label, query.date, result))
            prev = self.store.date_state(watch.id, query.date)
            decision = decide(prev, result, now)
            self._commit(watch, query.date, result, prev, decision, now)
            if decision.needs_confirmation:
                pending.append((watch, result, decision.state))
        return pending

    def _apply_confirmation(
        self,
        query: Query,
        pending: list[tuple[Watch, CheckResult, DateState]],
        answer: SearchAnswer,
        report: TickReport,
    ) -> None:
        now = self.clock()
        for watch, first, state in pending:
            recheck = evaluate(watch, query.date, answer)
            report.checks.append(CheckLine(watch.id, watch.label, query.date, recheck))
            decision = confirm_opening(state, first, recheck, now)
            shown = first if decision.alerts == (AlertKind.SEATS_OPEN_UNCONFIRMED,) else recheck
            self._commit(watch, query.date, recheck, state, decision, now, shown=shown)

    def _commit(
        self,
        watch: Watch,
        day: date,
        result: CheckResult,
        prev: DateState,
        decision: Decision,
        now: float,
        shown: CheckResult | None = None,
    ) -> None:
        url = get_site(watch.program).search_url(query_for(watch, day))
        alerts = []
        for kind in decision.alerts:
            subject, body = render_availability(
                kind, watch, day, shown or result, prev, now, url, self.headlines
            )
            # One email per person, so recipients never see each other's addresses.
            for recipient in watch.recipients or (None,):
                alerts.append(NewAlert(kind.value, subject, body, recipient=recipient))
        self.store.commit_check(watch.id, day, result, decision.state, alerts, now)

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
            days = upcoming_dates(watch, today)
            if not days:
                continue
            last_good = max(
                self.store.date_state(watch.id, d).last_conclusive_at or watch.created_at
                for d in days
            )
            # A slow watch is normally quiet for longer, and that alone isn't a problem.
            if now - last_good >= max(BLIND_AFTER_S, 2 * longest_gap_s(watch.checks_per_day)):
                hours = (now - last_good) / 3600
                self.store.enqueue(
                    NewAlert(
                        "blind",
                        f"jetbridge can't see {watch.flight.code}: no clear answer in "
                        f"{hours:.0f} hours",
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
                        "The raw responses are saved for repairing the parser.",
                        dedupe_key=f"drift:{site.id}:{int(float(since))}",
                    ),
                    now,
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
        self.store.set("last_digest", key)
        self.store.delete("longest_gap_s")

    def build_digest(self, watches: list[Watch]) -> tuple[str, str]:
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
            lines.append(f"{watch.label} via {get_site(watch.program).label} (watch {watch.id})")
            lines.append(f"  Seat alerts go to: {', '.join(watch.recipients) or 'you'}")
            lines.append(f"  Last 24 hours: {good} clear answers, {unknown} inconclusive")
            all_never_seen = True
            for day in upcoming_dates(watch, today):
                state = self.store.date_state(watch.id, day)
                if state.status is CheckStatus.AVAILABLE:
                    open_count += 1
                text = state.status.value.replace("_", " ") if state.status else "not checked yet"
                price = f", {state.miles:,} miles" if state.miles else ""
                lines.append(f"  {day:%b %d}: {text}{price}")
                if state.ever_listed or state.conclusive_count < NEVER_SEEN_AFTER:
                    all_never_seen = False
            if all_never_seen:
                lines.append(
                    f"  Note: {watch.flight.code} has never appeared in "
                    f"{domain}'s results for this route. The flight number or route may be "
                    f"wrong, {domain} may not sell this flight with these miles, or it may just "
                    "hide flights with no award space."
                )
            lines.append("")

        gap = float(self.store.get("longest_gap_s", "0") or 0)
        if gap:
            lines.append(
                f"This PC was off or asleep for up to {gap / 3600:.0f} hours since the "
                "last digest, so no checks ran during that time."
            )
        for site in SITES.values():
            until = self.governor.cooldown_until(site.id)
            if until > now:
                lines.append(f"{site.domain} is resting after a block until {format_when(until)}.")
        lines.append("If this daily email stops arriving, the watcher isn't running.")
        status = f"{open_count} date(s) OPEN" if open_count else "no seats yet"
        return f"jetbridge daily: {status}, still watching", "\n".join(lines)
