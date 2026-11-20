"""One-ticket connecting itineraries on an 'any' watch: counted only inside the watch's limits."""

from __future__ import annotations

import pytest
from conftest import DAY, answer_from, connecting_slice, cx_slice, make_runner, response
from typer.testing import CliRunner

from jetbridge import draft as drafts
from jetbridge import service
from jetbridge.cli import app
from jetbridge.matcher import connection_ok, evaluate, open_nonstops
from jetbridge.models import ANY_FLIGHT, Cabin, CheckStatus, Flight, Query, Watch


def watch(**kwargs) -> Watch:
    defaults = {"max_stops": 1, "max_layover_hours": 6, "max_total_hours": 24}
    return Watch(1, ANY_FLIGHT, "JFK", "HKG", (DAY,), Cabin.BUSINESS, **{**defaults, **kwargs})


def result(w: Watch, *slices: dict):
    return evaluate(w, DAY, answer_from(response(*slices)))


def test_a_connection_counts_when_the_watch_allows_one():
    found = result(watch(), connecting_slice())
    assert found.status is CheckStatus.AVAILABLE and found.miles == 80_000
    assert found.flight == "AA100+CX806" and found.flights == ("AA100+CX806",)
    assert any("via ORD" in note and "single ticket" in note for note in found.notes)


def test_a_nonstops_only_watch_still_ignores_connections():
    found = result(watch(max_stops=0), connecting_slice())
    assert found.status is CheckStatus.NOT_LISTED and "nonstop" in found.detail


def test_a_long_layover_does_not_count():
    assert result(watch(), connecting_slice(layover_h=8)).status is CheckStatus.NOT_LISTED
    longer = result(watch(max_layover_hours=8, max_total_hours=None), connecting_slice(layover_h=8))
    assert longer.status is CheckStatus.AVAILABLE


def test_a_trip_over_the_total_time_limit_does_not_count():
    assert result(watch(max_total_hours=18), connecting_slice()).status is CheckStatus.NOT_LISTED
    assert result(watch(max_total_hours=None), connecting_slice()).status is CheckStatus.AVAILABLE


def test_a_leg_in_another_cabin_never_counts():
    found = result(watch(), connecting_slice(second_cabin="COACH"))
    assert found.status is CheckStatus.NOT_OFFERED  # listed, but not all in business


def test_no_business_fare_is_not_offered():
    assert result(watch(), connecting_slice(business_miles=None)).status is CheckStatus.NOT_OFFERED


def test_the_price_limit_applies_to_a_connection_too():
    found = result(watch(max_miles=70_000), connecting_slice())
    assert found.status is CheckStatus.NOT_OFFERED and found.miles == 80_000


def test_the_cheapest_of_a_nonstop_and_a_connection_is_the_one_shown():
    found = result(watch(), cx_slice(business_miles=110_000), connecting_slice())
    assert found.flight == "AA100+CX806" and set(found.flights) == {"CX805", "AA100+CX806"}


def test_connection_ok_rejects_the_wrong_airports_and_dates():
    query = Query("JFK", "HKG", DAY)
    item = answer_from(response(connecting_slice())).parsed.slices[0]
    assert connection_ok(item, query, watch())
    assert not connection_ok(item, Query("EWR", "HKG", DAY), watch())
    assert not connection_ok(item, Query("JFK", "ICN", DAY), watch())


def test_scouting_lists_a_connection_with_where_it_connects():
    answer = answer_from(response(connecting_slice()))
    [found] = open_nonstops(watch(), Query("JFK", "HKG", DAY), answer)
    assert found.flight == "AA100+CX806" and found.via.startswith("via ORD, 2h layover")
    assert open_nonstops(watch(max_stops=0), Query("JFK", "HKG", DAY), answer) == []


# ---- saving and showing it -------------------------------------------------------------
def draft(**kwargs) -> drafts.WatchDraft:
    base = {
        "origin": "JFK",
        "destination": "HKG",
        "dates": "2027-05-21",
        "flight": "ANY",
        "max_stops": 1,
        "max_total_hours": 24,
    }
    return drafts.WatchDraft(**{**base, **kwargs})


def test_the_review_says_what_connections_mean():
    text = drafts.review_text(draft(), "me@x.com")
    assert "Connections: up to 1 stop, layovers up to 6 h, 24 h total at most" in text
    assert "One ticket only" in text


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"flight": "CX805"}, "only work when watching any flight"),
        ({"max_stops": 2}, "0 or 1 stop"),
        ({"max_layover_hours": 20}, "1 to 12 hours"),
        ({"max_total_hours": 90}, "1 to 48 hours"),
    ],
)
def test_unreasonable_limits_are_refused(kwargs, message):
    with pytest.raises(ValueError, match=message):
        drafts.validate(draft(**kwargs))


def test_a_saved_watch_keeps_its_limits(store):
    saved = drafts.save(store, draft(max_layover_hours=4))
    assert (saved.max_stops, saved.max_total_hours, saved.max_layover_hours) == (1, 24, 4)
    plain = drafts.save(store, draft(max_stops=0, max_total_hours=None))
    assert plain.max_stops == 0 and plain.max_total_hours is None


def test_the_command_saves_a_watch_with_connections(store, monkeypatch):
    monkeypatch.setattr(service, "open_store", lambda: store)
    args = [
        "watch", "add", "any", "--from", "JFK", "--to", "HKG", "--dates", "2027-05-21",
        "--stops", "1", "--max-hours", "20", "--max-layover", "4", "--notify", "me", "--yes",
    ]  # fmt: skip
    outcome = CliRunner().invoke(app, args)
    assert outcome.exit_code == 0, outcome.output
    [saved] = store.list_watches()
    assert (saved.max_stops, saved.max_total_hours, saved.max_layover_hours) == (1, 20, 4)


# ---- through a real tick ---------------------------------------------------------------
async def test_a_connection_opening_sends_the_usual_alert_with_its_path(store, clock):
    created = store.add_watch(Flight("ANY", ""), "JFK", "HKG", (DAY,), Cabin.BUSINESS, now=clock())
    store.set_connections(created.id, 1, 24, 6)
    open_answer = answer_from(response(connecting_slice()))
    runner, _, sender, _ = make_runner(store, clock, [open_answer, open_answer])
    await runner.tick()
    subject, body = next((s, b) for s, b in sender.sent if s.startswith("SEATS OPEN"))
    assert "AA100+CX806" in subject and "80,000 mi" in subject
    assert "via ORD" in body
