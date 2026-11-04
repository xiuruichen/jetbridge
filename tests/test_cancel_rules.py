"""What seat alerts say about cancelling."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from conftest import DAY

from jetbridge.acquire.rules import CANCEL_RULES, MAX_AGE_DAYS, cancel_note
from jetbridge.acquire.sites import PROGRAMS
from jetbridge.alerts import AlertKind, DateState, render_availability
from jetbridge.models import Cabin, CheckResult, CheckStatus, Flight, Watch

NOV = date(2026, 11, 3)


def test_every_program_has_rules():
    assert set(CANCEL_RULES) == set(PROGRAMS)


@pytest.mark.parametrize("program", PROGRAMS)
def test_rules_get_checked_again_within_a_year(program):
    """This fails once a rule is too old, which is the reminder to read the airline's page again."""
    assert (date.today() - CANCEL_RULES[program].checked).days <= MAX_AGE_DAYS


def test_a_confirmed_rule_is_stated_with_its_source():
    note = cancel_note("delta", NOV)
    assert "no redeposit or change fee" in note and "Basic fares" in note
    assert CANCEL_RULES["delta"].url in note


@pytest.mark.parametrize("program", ["american", "united"])
def test_an_unconfirmed_rule_is_only_a_pointer(program):
    note = cancel_note(program, NOV)
    assert "current cancel rules" in note and CANCEL_RULES[program].url in note
    assert "fee" not in note


def test_a_rule_too_old_is_no_longer_stated():
    late = CANCEL_RULES["delta"].checked + timedelta(days=MAX_AGE_DAYS + 1)
    note = cancel_note("delta", late)
    assert "no redeposit" not in note and CANCEL_RULES["delta"].url in note


def _watch(program: str = "delta") -> Watch:
    return Watch(1, Flight("DL", "197"), "SEA", "ICN", (DAY,), Cabin.BUSINESS, program=program)


def test_seat_alerts_say_what_to_do_but_a_gone_alert_does_not():
    result = CheckResult(CheckStatus.AVAILABLE, miles=95_000)
    _, body = render_availability(
        AlertKind.SEATS_OPEN,
        _watch(),
        DAY,
        result,
        DateState(),
        1_800_000_000.0,
        "https://example.com",
    )
    assert "If plans change:" in body
    _, gone = render_availability(
        AlertKind.SEATS_GONE,
        _watch(),
        DAY,
        CheckResult(CheckStatus.NOT_OFFERED),
        DateState(),
        1_800_000_000.0,
        "https://example.com",
    )
    assert "If plans change:" not in gone
