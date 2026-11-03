"""Keeps the pacing honest: adaptive must not give anything up when there's no pattern at all."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_path = Path(__file__).parent.parent / "tools" / "simulate.py"
_spec = importlib.util.spec_from_file_location("simulate", _path)
simulate = importlib.util.module_from_spec(_spec)
sys.modules["simulate"] = simulate
_spec.loader.exec_module(simulate)


@pytest.fixture(scope="module")
def results():
    simulate.RUNS = 100
    return simulate.compare()


def test_no_pattern_at_all_costs_little(results):
    row = results["no pattern at all"]
    assert row["adaptive_caught"] >= row["fixed_caught"] - 0.04


def test_it_helps_when_seats_open_late(results):
    row = results["mostly in the last two weeks"]
    assert row["adaptive_caught"] > row["fixed_caught"] + 0.03


def test_comparisons_use_about_the_same_searches(results):
    for row in results.values():
        assert abs(row["adaptive_checks"] - row["fixed_checks"]) / row["fixed_checks"] < 0.1


def test_results_are_labeled_as_simulated(capsys):
    simulate.RUNS = 2
    simulate.main()
    assert "SIMULATED" in capsys.readouterr().out
