"""Full flow, offline: real parser, store, rules and outbox, with a scripted site and clock."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from conftest import DAY, FakeSender, add_cx, answer_from, cx_slice, make_runner, response

from jetbridge.acquire import american
from jetbridge.acquire.fetcher import interpret
from jetbridge.acquire.types import RawResponse
from jetbridge.models import AnswerKind, CheckStatus, SearchAnswer

CLOSED = response(cx_slice(business_miles=None))
OPEN = response(cx_slice(seats=2))
DAY2 = DAY + timedelta(days=1)


def blocked(until: float) -> SearchAnswer:
    return SearchAnswer(AnswerKind.BLOCKED, detail="challenge page markers", retry_at=until)


async def test_closed_open_confirmed_gone_blocked(store, clock):
    watch = add_cx(store, clock)
    runner, fetcher, sender, _ = make_runner(
        store,
        clock,
        [
            answer_from(CLOSED),  # closed
            answer_from(OPEN),  # opens...
            answer_from(OPEN),  # ...and the re-check confirms
            answer_from(CLOSED),  # gone
            blocked(clock() + 10**7),
        ],
    )

    await runner.tick()
    assert sender.sent == []

    clock.now += 86400 / 6 * 1.5
    await runner.tick()
    assert len(sender.sent) == 1
    subject, body = sender.sent[0]
    assert subject == "SEATS OPEN: CX805 business on May 21, 2027 - 110,000 mi"
    assert "110,000 miles + USD 5.60 per person" in body
    assert "Seats:    2 (searched for 1)" in body
    assert "https://www.aa.com/booking/search?" in body and "searchType=Award" in body

    clock.now += 86400 / 6 * 1.5
    await runner.tick()
    assert sender.subjects[1] == "Seats gone: CX805 business on May 21, 2027"

    clock.now += 86400 / 6 * 1.5
    report = await runner.tick()
    assert len(sender.sent) == 2, "a block must not produce a seats alert"
    assert report.checks[-1].result.status is CheckStatus.UNKNOWN
    assert store.date_state(watch.id, DAY).status is CheckStatus.NOT_OFFERED
    assert len(fetcher.queries) == 5
    assert fetcher.closed == 4


async def test_flicker_sends_nothing_then_real_opening_alerts(store, clock):
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(
        store, clock, [answer_from(OPEN), answer_from(CLOSED), answer_from(OPEN), answer_from(OPEN)]
    )
    await runner.tick()
    assert sender.sent == []
    clock.now += 86400
    await runner.tick()
    assert [s.split(":")[0] for s in sender.subjects] == ["SEATS OPEN"]


async def test_blocked_recheck_sends_unconfirmed_alert(store, clock):
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(OPEN), blocked(clock() + 3600)])
    await runner.tick()
    assert sender.subjects[0].startswith("SEATS MAY BE OPEN (unconfirmed)")
    assert "110,000" in sender.subjects[0]


async def test_block_stops_all_requests_and_reschedules_after_rest(store, clock):
    watch = add_cx(store, clock, dates=(DAY, DAY2, DAY2 + timedelta(days=1)))
    until = clock() + 600
    runner, fetcher, _, _ = make_runner(store, clock, [blocked(until)])
    report = await runner.tick()

    assert len(fetcher.queries) == 1, "no request may follow a block"
    assert [c.result.status for c in report.checks] == [CheckStatus.UNKNOWN] * 3
    assert len(store.history(watch.id)) == 3, "every date gets an outcome"
    assert store.get_watch(watch.id).next_check_at >= until


async def test_requests_are_spaced_out(store, clock):
    add_cx(store, clock, dates=(DAY, DAY2))
    start = clock()
    runner, _, _, _ = make_runner(store, clock, [answer_from(CLOSED), answer_from(CLOSED)])
    await runner.tick()
    assert 20 <= clock() - start <= 60


async def test_one_catch_up_after_long_downtime(store, clock):
    add_cx(store, clock)
    runner, fetcher, _, _ = make_runner(store, clock, [answer_from(CLOSED), answer_from(CLOSED)])
    await runner.tick()
    clock.now += 5 * 86400
    await runner.tick()
    await runner.tick()
    assert len(fetcher.queries) == 2


async def test_shared_route_uses_one_request(store, clock):
    a = add_cx(store, clock)
    b = add_cx(store, clock)
    runner, fetcher, _, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    assert len(fetcher.queries) == 1
    assert store.history(a.id) and store.history(b.id)


async def test_email_outage_loses_nothing(store, clock):
    add_cx(store, clock)
    sender = FakeSender()
    sender.failing = True
    runner, _, _, _ = make_runner(store, clock, [answer_from(OPEN), answer_from(OPEN)], sender)
    await runner.tick()
    assert sender.sent == [] and len(store.pending_alerts()) == 1
    sender.failing = False
    await runner.tick()
    assert sender.subjects[0].startswith("SEATS OPEN")


async def test_blind_warning_fires_once_per_episode(store, clock):
    add_cx(store, clock)
    answers = [SearchAnswer(AnswerKind.ERROR, detail="network") for _ in range(12)]
    runner, _, sender, _ = make_runner(store, clock, answers)
    for _ in range(12):
        clock.now += 86400 / 4
        await runner.tick()
    blind = [s for s in sender.subjects if "can't see" in s]
    assert len(blind) == 1 and "CX805" in blind[0]


async def test_drift_alerts_after_two_in_a_row_and_clears(store, clock):
    add_cx(store, clock)
    drift = SearchAnswer(AnswerKind.DRIFT, detail="no ng-state")
    runner, _, sender, _ = make_runner(store, clock, [drift, answer_from(CLOSED), drift, drift])
    for _ in range(3):
        await runner.tick()
        clock.now += 86400 / 3
    assert not [s for s in sender.subjects if "format" in s]
    await runner.tick()
    assert [s for s in sender.subjects if "format" in s]


async def test_expired_watch_finishes_and_makes_no_requests(store, clock):
    watch = add_cx(store, clock, dates=(date(2027, 1, 1),))
    runner, fetcher, _, _ = make_runner(store, clock, [])
    await runner.tick()
    assert store.get_watch(watch.id).status == "finished" and fetcher.queries == []


async def test_paused_watch_makes_no_requests(store, clock):
    watch = add_cx(store, clock)
    store.set_watch_status(watch.id, "paused")
    runner, fetcher, _, _ = make_runner(store, clock, [])
    await runner.tick()
    assert fetcher.queries == []


async def test_digest_daily_with_never_seen_and_offline_notes(store, clock):
    store.set("digest_enabled", "1")
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(response()) for _ in range(9)])
    for _ in range(7):
        clock.now += 86400 / 6 * 1.5
        await runner.tick()
    digests = [(s, b) for s, b in sender.sent if s.startswith("jetbridge daily")]
    assert 1 <= len(digests) <= 3

    # Land after the morning digest hour, past the 6-hour gap that counts as offline.
    clock.now += 86400 * 2 + 6 * 3600
    assert datetime.fromtimestamp(clock()).hour >= 8
    await runner.tick()
    subject, body = next(
        (s, b) for s, b in reversed(sender.sent) if s.startswith("jetbridge daily")
    )
    assert subject == "jetbridge daily: no seats yet, still watching"
    assert "never appeared" in body
    assert "off or asleep" in body
    assert "If this daily email stops arriving" in body


async def test_digest_can_be_turned_off(store, clock):
    add_cx(store, clock)
    runner, _, sender, _ = make_runner(store, clock, [answer_from(CLOSED)])
    await runner.tick()
    assert sender.sent == []


def test_interpret_block_page_opens_cooldown_and_never_reads_as_answer(store, clock):
    from conftest import block_page

    from jetbridge.acquire.governor import Governor

    gov = Governor(store, clock, clock.sleep)
    saved = []
    answer = interpret(
        RawResponse(200, block_page()), gov, "american", lambda _raw, _label: saved.append(_label)
    )
    assert answer.kind is AnswerKind.BLOCKED and saved == ["blocked"]
    assert gov.cooldown_until("american") == clock() + 600


def test_interpret_clears_probe_only_on_real_answer(store, clock):
    from conftest import to_html

    from jetbridge.acquire.governor import Governor

    gov = Governor(store, clock, clock.sleep)
    gov.record_block("american")
    assert gov.health("american").needs_probe
    interpret(RawResponse(200, "<html>odd</html>"), gov, "american", lambda _raw, _label: None)
    assert gov.health("american").needs_probe
    answer = interpret(
        RawResponse(200, to_html(response())), gov, american.SITE, lambda _raw, _label: None
    )
    assert answer.kind is AnswerKind.ANSWER and not gov.health("american").needs_probe


@pytest.mark.parametrize("checks_per_day", [1, 2])
async def test_a_slow_watch_on_schedule_is_not_called_blind(store, clock, checks_per_day):
    """At 1 check a day, the next one can be up to 31 hours away. That's normal, not a problem."""
    state = response(cx_slice(business_miles=None))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(state)])
    add_cx(store, clock, checks_per_day=checks_per_day)
    await runner.tick()
    clock.now += 25 * 3600  # past 24 hours, but within this watch's normal gap
    store.set_next_check(1, clock.now + 6 * 3600)
    await runner.tick()
    assert not [s for s in sender.subjects if "can't see" in s]


async def test_a_slow_watch_is_blind_after_two_missed_gaps(store, clock):
    state = response(cx_slice(business_miles=None))
    runner, _, sender, _ = make_runner(store, clock, [answer_from(state)])
    add_cx(store, clock, checks_per_day=1)
    await runner.tick()
    clock.now += 63 * 3600  # twice the longest gap (31.2 hours) has gone by with no answer
    store.set_next_check(1, clock.now + 3600)
    await runner.tick()
    assert [s for s in sender.subjects if "can't see" in s]
