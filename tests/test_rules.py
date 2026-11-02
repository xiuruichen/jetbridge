from __future__ import annotations

import random
from dataclasses import replace
from datetime import date

import pytest
from conftest import DAY, START, FakeSender, add_cx

from jetbridge.acquire.governor import Governor, GovernorDeferred
from jetbridge.alerts import AlertKind, DateState, confirm_opening, decide
from jetbridge.models import Cabin, CheckResult, CheckStatus, Flight, Watch
from jetbridge.notify.email import flush_outbox
from jetbridge.scheduler import group_queries, is_due, is_expired, next_check_at
from jetbridge.service import create_watch, parse_dates
from jetbridge.store import NewAlert

OPEN = CheckResult(CheckStatus.AVAILABLE, miles=110_000, seats_exact=2)
CLOSED = CheckResult(CheckStatus.NOT_OFFERED)
UNLISTED = CheckResult(CheckStatus.NOT_LISTED)
UNKNOWN = CheckResult(CheckStatus.UNKNOWN, detail="blocked")
OPEN_STATE = DateState(status=CheckStatus.AVAILABLE, miles=110_000, seats_exact=2, last_alert_at=0)


# ---- alert transitions ----------------------------------------------------------------
def test_opening_needs_confirmation_and_stays_closed():
    d = decide(DateState(status=CheckStatus.NOT_OFFERED), OPEN, 10)
    assert d.needs_confirmation and not d.alerts and d.state.status is CheckStatus.NOT_OFFERED


def test_first_ever_check_open_also_needs_confirmation():
    assert decide(DateState(), OPEN, 10).needs_confirmation


@pytest.mark.parametrize(
    "state", [DateState(), OPEN_STATE, DateState(status=CheckStatus.NOT_OFFERED)]
)
def test_unknown_never_alerts_or_changes_status(state):
    d = decide(state, UNKNOWN, 10)
    assert not d.alerts and not d.needs_confirmation
    assert d.state.status == state.status and d.state.miles == state.miles
    assert d.state.last_conclusive_at == state.last_conclusive_at


def test_open_then_closed_alerts_gone():
    d = decide(OPEN_STATE, CLOSED, 10)
    assert d.alerts == (AlertKind.SEATS_GONE,) and d.state.status is CheckStatus.NOT_OFFERED
    assert d.state.miles is None


def test_closed_stays_quiet():
    assert not decide(DateState(status=CheckStatus.NOT_OFFERED), CLOSED, 10).alerts


def test_price_drop_alerts_once():
    d = decide(OPEN_STATE, replace(OPEN, miles=90_000), 10)
    assert d.alerts == (AlertKind.PRICE_DROP,) and d.state.miles == 90_000
    assert not decide(d.state, replace(OPEN, miles=90_000), 20).alerts


def test_price_rise_is_silent_but_recorded():
    d = decide(OPEN_STATE, replace(OPEN, miles=130_000), 10)
    assert not d.alerts and d.state.miles == 130_000


def test_more_seats_alerts_but_fewer_does_not():
    assert decide(OPEN_STATE, replace(OPEN, seats_exact=4), 10).alerts == (AlertKind.MORE_SEATS,)
    assert not decide(OPEN_STATE, replace(OPEN, seats_exact=1), 10).alerts


def test_reminder_only_after_24_hours():
    assert not decide(OPEN_STATE, OPEN, 86_399).alerts
    assert decide(OPEN_STATE, OPEN, 86_400).alerts == (AlertKind.REMINDER,)


def test_confirmation_outcomes():
    state = decide(DateState(), OPEN, 10).state
    good = confirm_opening(state, OPEN, OPEN, 20)
    assert good.alerts == (AlertKind.SEATS_OPEN,) and good.state.status is CheckStatus.AVAILABLE

    unsure = confirm_opening(state, OPEN, UNKNOWN, 20)
    assert unsure.alerts == (AlertKind.SEATS_OPEN_UNCONFIRMED,)
    assert unsure.state.status is CheckStatus.AVAILABLE and unsure.state.miles == 110_000

    flicker = confirm_opening(state, OPEN, CLOSED, 20)
    assert not flicker.alerts and flicker.state.status is CheckStatus.NOT_OFFERED


def test_ever_listed_tracking():
    assert not decide(DateState(), UNLISTED, 1).state.ever_listed
    assert decide(DateState(), CLOSED, 1).state.ever_listed


# ---- schedule -------------------------------------------------------------------------
def test_jitter_stays_in_bounds_and_varies():
    rng = random.Random(3)
    gaps = [next_check_at(0, 6, rng) for _ in range(200)]
    assert all(4 * 3600 * 0.7 <= g <= 4 * 3600 * 1.3 for g in gaps) and len(set(gaps)) > 150


def _watch(**kw) -> Watch:
    base = dict(
        id=1,
        flight=Flight("CX", "805"),
        origin="JFK",
        destination="HKG",
        dates=(DAY,),
        cabin=Cabin.BUSINESS,
    )
    return Watch(**{**base, **kw})


def test_grouping_shares_requests_only_when_identical():
    groups = group_queries(
        [_watch(id=1), _watch(id=2, cabin=Cabin.FIRST), _watch(id=3, pax=3)], date(2027, 1, 1)
    )
    assert sorted(len(v) for v in groups.values()) == [1, 2]
    assert {q.pax for q in groups} == {1, 3}


def test_past_dates_are_skipped_and_expiry():
    w = _watch(dates=(date(2027, 1, 1), DAY))
    assert [q.date for q in group_queries([w], date(2027, 2, 1))] == [DAY]
    assert not is_expired(w, DAY) and is_expired(w, date(2027, 5, 22))


def test_paused_is_never_due():
    assert not is_due(_watch(status="paused"), 10) and is_due(_watch(next_check_at=5), 10)


def test_parse_dates():
    assert parse_dates("2027-05-21..2027-05-23") == (
        date(2027, 5, 21),
        date(2027, 5, 22),
        date(2027, 5, 23),
    )
    assert parse_dates("2027-05-23, 2027-05-21") == (date(2027, 5, 21), date(2027, 5, 23))
    for bad in ("2027-05-23..2027-05-21", "2027-01-01..2027-03-01", "soon", ""):
        with pytest.raises(ValueError):
            parse_dates(bad)


@pytest.mark.parametrize(
    "change",
    [
        {"flight": "CX"},
        {"origin": "JFKX"},
        {"destination": "jfk"},
        {"program": "delta"},
        {"pax": 0},
        {"checks_per_day": 0},
        {"cabin": "luxury"},
        {"dates": "2020-01-01"},
    ],
)
def test_create_watch_rejects_bad_input(store, clock, change):
    args = dict(
        flight="CX805", origin="JFK", destination="HKG", dates="2027-05-21", cabin="business"
    )
    with pytest.raises(ValueError):
        create_watch(store, **{**args, **change}, now=clock())


def test_create_watch_normalizes(store, clock):
    w = create_watch(
        store,
        flight="cx 0805",
        origin="jfk",
        destination="hkg",
        dates="2027-05-21",
        cabin="Business",
        now=clock(),
    )
    assert (w.flight.code, w.origin, w.cabin) == ("CX805", "JFK", Cabin.BUSINESS)


# ---- governor -------------------------------------------------------------------------
async def test_budget_blocks_the_eleventh_request(store, clock):
    gov = Governor(store, clock, clock.sleep)
    for _ in range(10):
        await gov.acquire("american", clock() + 1)
    with pytest.raises(GovernorDeferred):
        await gov.acquire("american", clock() + 1)


async def test_budget_waits_when_deadline_allows(store, clock):
    gov = Governor(store, clock, clock.sleep)
    for _ in range(10):
        await gov.acquire("american", clock() + 1)
    await gov.acquire("american", clock() + 700)
    assert clock() >= START + 600


async def test_cooldown_doubles_is_capped_and_persists(store, clock):
    gov = Governor(store, clock, clock.sleep)
    lengths = []
    for _ in range(7):
        until = gov.record_block("american")
        lengths.append(until - clock())
        clock.now = until + 1
    assert lengths == [600, 1200, 2400, 4800, 9600, 14400, 14400]

    gov.record_block("american")
    with pytest.raises(GovernorDeferred):
        await Governor(store, clock, clock.sleep).acquire("american", clock() + 99_999)


async def test_episodes_reset_after_quiet_period(store, clock):
    gov = Governor(store, clock, clock.sleep)
    gov.record_block("american")
    clock.now += 7 * 3600
    assert gov.record_block("american") - clock() == 600


# ---- outbox ---------------------------------------------------------------------------
def test_outbox_retries_in_order_after_failure(store, clock):
    sender = FakeSender()
    store.enqueue(NewAlert("a", "first", "b"), clock())
    store.enqueue(NewAlert("a", "second", "b"), clock())
    sender.failing = True
    assert flush_outbox(store, sender, clock()) == 0
    assert len(store.pending_alerts()) == 2
    sender.failing = False
    assert flush_outbox(store, sender, clock()) == 2
    assert sender.subjects == ["first", "second"] and not store.pending_alerts()


def test_dedupe_key_sends_once(store, clock):
    sender = FakeSender()
    for _ in range(3):
        store.enqueue(NewAlert("blind", "x", "y", dedupe_key="k"), clock())
    flush_outbox(store, sender, clock())
    store.enqueue(NewAlert("blind", "x", "y", dedupe_key="k"), clock())
    flush_outbox(store, sender, clock())
    assert sender.subjects == ["x"]


def test_stale_digest_is_dropped_but_real_alert_is_kept(store, clock):
    sender = FakeSender()
    store.enqueue(NewAlert("digest", "old digest", "b"), clock())
    store.enqueue(NewAlert("seats_open", "SEATS OPEN", "b"), clock())
    clock.now += 2 * 86400
    flush_outbox(store, sender, clock())
    assert sender.subjects == ["SEATS OPEN"]


def test_commit_is_atomic(store, clock):
    w = add_cx(store, clock)
    bad = DateState(status=CheckStatus.AVAILABLE)
    with pytest.raises(Exception):  # noqa: B017
        store.commit_check(w.id, DAY, OPEN, bad, [NewAlert("k", None, "b")], clock())  # type: ignore[arg-type]
    assert store.date_state(w.id, DAY) == DateState()
    assert not store.history(w.id)
