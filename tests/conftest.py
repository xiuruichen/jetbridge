from __future__ import annotations

import json
import random
from copy import deepcopy
from datetime import date, datetime
from pathlib import Path

import pytest

from jetbridge.acquire.governor import Governor
from jetbridge.models import AnswerKind, Cabin, Flight, Query, SearchAnswer
from jetbridge.runner import Runner
from jetbridge.store import Store

FIXTURES = Path(__file__).parent / "fixtures"
DAY = date(2027, 5, 21)
START = datetime(2027, 1, 10, 9, 0).timestamp()


def to_html(state: dict) -> str:
    return (
        f'<html><script id="ng-state" type="application/json">{json.dumps(state)}</script></html>'
    )


@pytest.fixture
def aa_state() -> dict:
    path = FIXTURES / "american" / "dfw_hnd_award_20270528.ngstate.json"
    return json.loads(path.read_text(encoding="utf-8"))


def _pricing(cabin_type: str, miles: int, seats: int = 0, available: bool = True) -> dict:
    return {
        "productType": cabin_type,
        "productAvailable": available,
        "perPassengerAwardPoints": miles,
        "perPassengerTaxesAndFees": {"amount": 5.6, "currency": "USD"},
        "seatsRemaining": seats,
    }


def cx_slice(
    *,
    business_miles: int | None = 110_000,
    seats: int = 0,
    leg_cabin: str = "BUSINESS",
    flight: str = "805",
    day: str = "2027-05-21",
) -> dict:
    """A CX805 itinerary in the shape of a recorded aa.com response."""
    products = [_pricing("COACH", 60_000)]
    if business_miles is not None:
        products.append(_pricing("BUSINESS", business_miles, seats))
    return {
        "segments": [
            {
                "flight": {"carrierCode": "CX", "flightNumber": flight},
                "legs": [
                    {
                        "flight": None,
                        "origin": {"code": "JFK"},
                        "destination": {"code": "HKG"},
                        "departureDateTime": f"{day}T01:00:00.000-04:00",
                        "arrivalDateTime": f"{day}T23:00:00.000+08:00",
                        "aircraftCode": "77W",
                        "operationalDisclosure": "",
                        "productDetails": [
                            {"productType": "COACH", "cabinType": "COACH"},
                            {"productType": "BUSINESS", "cabinType": leg_cabin},
                        ],
                    }
                ],
            }
        ],
        "pricingDetail": products,
    }


def response(*slices: dict, error: str = "") -> dict:
    return {
        "SearchData": {
            "itineraryResult": {
                "error": error,
                "responseMetadata": {"cached": False},
                "slices": list(slices),
            }
        }
    }


def block_page() -> str:
    return "<html><title>Challenge Validation</title>Please confirm you're a human</html>"


class FakeClock:
    def __init__(self, start: float = START):
        self.now = start

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


class ScriptedFetcher:
    """Plays back real-parser answers in order. Each entry is a SearchAnswer."""

    def __init__(self, answers: list[SearchAnswer]):
        self.answers = deepcopy(answers)
        self.queries: list[Query] = []
        self.closed = 0

    async def search(self, query: Query, deadline: float) -> SearchAnswer:
        self.queries.append(query)
        if not self.answers:
            raise AssertionError(f"unexpected extra request: {query}")
        return self.answers.pop(0)

    async def close(self) -> None:
        self.closed += 1


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []
        self.failing = False

    def send(self, subject: str, body: str) -> None:
        if self.failing:
            raise OSError("smtp down")
        self.sent.append((subject, body))

    @property
    def subjects(self) -> list[str]:
        return [s for s, _ in self.sent]


def answer_from(state: dict) -> SearchAnswer:
    from jetbridge.acquire import american

    return SearchAnswer(AnswerKind.ANSWER, parsed=american.parse(to_html(state)))


@pytest.fixture
def store(tmp_path) -> Store:
    s = Store(tmp_path / "t.db")
    s.set("digest_enabled", "0")
    return s


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def make_runner(store, clock, answers, sender=None):
    fetcher = ScriptedFetcher(answers)
    sender = sender or FakeSender()
    governor = Governor(store, clock, clock.sleep)
    runner = Runner(
        store, fetcher, sender, governor, clock=clock, sleep=clock.sleep, rng=random.Random(1)
    )
    return runner, fetcher, sender, governor


def add_cx(store, clock, dates=(DAY,), **kwargs):
    return store.add_watch(
        Flight("CX", "805"), "JFK", "HKG", tuple(dates), Cabin.BUSINESS, now=clock(), **kwargs
    )
