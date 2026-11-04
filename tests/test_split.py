"""A family willing to split across nonstops."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import DAY, answer_from, cx_slice, make_runner, response

from jetbridge import draft, service
from jetbridge.alerts import AlertKind, DateState, render_availability
from jetbridge.matcher import evaluate
from jetbridge.models import ANY_FLIGHT, Cabin, CheckResult, CheckStatus, Watch

FUTURE = (date.today() + timedelta(days=60)).isoformat()
WATCH = Watch(1, ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, pax=2, whole_party=4)


def flights(*numbers: str, seats: int = 0):
    return answer_from(response(*(cx_slice(flight=n, seats=seats) for n in numbers)))


def test_one_flight_with_two_seats_does_not_count():
    result = evaluate(WATCH, DAY, flights("805"))
    assert result.status is CheckStatus.NOT_OFFERED
    assert "seats for 2 of your 4 so far, on CX805" in result.detail
    assert result.miles is None  # not a near miss, so it isn't reported as above a price limit


def test_two_flights_seat_everyone():
    result = evaluate(WATCH, DAY, flights("805", "889"))
    assert result.status is CheckStatus.AVAILABLE and result.flights == ("CX805", "CX889")


def test_one_flight_with_enough_seats_counts():
    result = evaluate(WATCH, DAY, flights("805", seats=4))
    assert result.status is CheckStatus.AVAILABLE


def test_a_party_of_five_needs_three_groups_of_two():
    five = Watch(1, ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, pax=2, whole_party=5)
    assert evaluate(five, DAY, flights("805", "889")).status is CheckStatus.NOT_OFFERED
    assert evaluate(five, DAY, flights("805", "889", "811")).status is CheckStatus.AVAILABLE


def test_a_plain_any_flight_watch_still_alerts_on_one_flight():
    plain = Watch(1, ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, pax=2)
    assert evaluate(plain, DAY, flights("805")).status is CheckStatus.AVAILABLE


async def test_no_alert_if_a_flight_is_gone_on_the_re_check(store, clock):
    store.add_watch(
        ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock(), pax=2, whole_party=4
    )
    answers = [flights("805", "889"), flights("805")]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    assert not [s for s in sender.subjects if s.startswith("SEATS OPEN")]


async def test_it_alerts_after_the_re_check_when_both_are_still_there(store, clock):
    store.add_watch(
        ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock(), pax=2, whole_party=4
    )
    answers = [flights("805", "889"), flights("805", "889")]
    runner, _, sender, _ = make_runner(store, clock, answers)
    await runner.tick()
    body = next(b for s, b in sender.sent if s.startswith("SEATS OPEN"))
    assert "Also open: CX889" in body and "separate bookings" in body


def test_the_alert_warns_about_separate_bookings():
    result = CheckResult(
        CheckStatus.AVAILABLE, miles=95_000, flight="CX805", flights=("CX805", "CX889")
    )
    _, body = render_availability(
        AlertKind.SEATS_OPEN, WATCH, DAY, result, DateState(), 1_800_000_000.0, "https://x.test"
    )
    assert "cancelling one booking doesn't change another" in body


@pytest.mark.parametrize(
    ("fields", "problem"),
    [
        ({"flight": "CX805"}, "needs any nonstop"),
        ({"whole_party": 2}, "more than one group"),
        ({"whole_party": 10}, "at most 9"),
    ],
)
def test_it_has_to_make_sense(fields, problem):
    base = {
        "flight": "any",
        "origin": "JFK",
        "destination": "HKG",
        "dates": FUTURE,
        "cabin": "business",
        "pax": 2,
        "whole_party": 4,
    }
    with pytest.raises(ValueError, match=problem):
        service.validate_watch(**{**base, **fields})


def test_it_is_saved_and_survives_an_edit(store):
    watch = service.create_watch(
        store,
        flight="any",
        origin="JFK",
        destination="HKG",
        dates=FUTURE,
        cabin="business",
        pax=2,
        whole_party=4,
    )
    changed = service.change_watch(store, watch, dates=FUTURE, checks_per_day=4, max_miles=None)
    assert changed.whole_party == 4


def test_the_review_says_how_it_splits():
    text = draft.review_text(
        draft.WatchDraft("JFK", "HKG", FUTURE, "ANY", pax=2, whole_party=4), "me@x.com"
    )
    assert "Passengers:  4, on separate bookings of 2" in text
