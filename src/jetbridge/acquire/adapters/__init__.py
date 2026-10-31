"""Adapter registry."""

from __future__ import annotations

from jetbridge.acquire.adapters.alaska import AlaskaAdapter
from jetbridge.acquire.adapters.american import AmericanAdapter
from jetbridge.acquire.adapters.base import Adapter
from jetbridge.acquire.adapters.delta import DeltaAdapter
from jetbridge.acquire.adapters.google_flights import GoogleFlightsAdapter
from jetbridge.acquire.adapters.united import UnitedAdapter

_ALL: list[type[Adapter]] = [
    AlaskaAdapter,
    AmericanAdapter,
    DeltaAdapter,
    GoogleFlightsAdapter,
    UnitedAdapter,
]


def registry() -> dict[str, Adapter]:
    return {cls.id: cls() for cls in _ALL}


def award_adapters_for_program(program: str) -> list[str]:
    return [cls.id for cls in _ALL if cls.program == program]
