"""Behavioral contracts for pure per-symbol selection power."""
from __future__ import annotations

import math
import statistics
from typing import Final

import pytest

from app.domain.strategy_v2.selection_power import (
    assess_selection_power,
    reach_gate_operating_point,
    required_trades_one_sample,
    required_trades_two_sample,
)

# Twelve symmetric observations with sample standard deviation 54 bps.
RETURNS: Final = (-54 * math.sqrt(11 / 12), 54 * math.sqrt(11 / 12)) * 6


def test_required_trades_one_sample_reproduces_46() -> None:
    # Given / When
    result = required_trades_one_sample(sigma_bps=54, delta_bps=20)
    # Then
    assert result == 46


def test_two_sample_is_exactly_double() -> None:
    # Given
    for sigma, delta, alpha, power in (
        (54, 20, 0.05, 0.80), (30, 10, 0.01, 0.90), (75, 15, 0.10, 0.95),
    ):
        expected = required_trades_one_sample(
            sigma_bps=sigma, delta_bps=delta, alpha=alpha, power=power,
        )
        # When
        result = required_trades_two_sample(
            sigma_bps=sigma, delta_bps=delta, alpha=alpha, power=power,
        )
        # Then
        assert result == 2 * expected


def test_verdict_unpowered_when_max_held_below_required() -> None:
    # Given / When
    result = assess_selection_power(
        per_trade_returns_bps=RETURNS, held_by_symbol={"AMD.US": 4}, delta_bps=20,
    )
    # Then
    assert result.verdict == "UNPOWERED"
    assert result.shortfall_factor == pytest.approx(46 / 4)
    assert (result.max_trades_symbol, result.max_trades_held) == ("AMD.US", 4)
    assert (result.required_one_sample, result.required_two_sample) == (46, 92)


def test_verdict_powered_when_some_symbol_meets_required() -> None:
    # Given / When
    result = assess_selection_power(
        per_trade_returns_bps=RETURNS,
        held_by_symbol={"AMD.US": 4, "X.US": 50}, delta_bps=20,
    )
    # Then
    assert result.verdict == "POWERED"
    assert (result.max_trades_symbol, result.max_trades_held) == ("X.US", 50)


def test_verdict_unmeasurable_below_sigma_floor() -> None:
    # Given / When
    result = assess_selection_power(
        per_trade_returns_bps=RETURNS[:9], held_by_symbol={"X.US": 50}, delta_bps=20,
    )
    # Then
    assert result.sigma_bps is None
    assert result.sigma_observations == 9
    assert result.verdict == "UNMEASURABLE"
    assert result.required_one_sample is None
    assert result.required_two_sample is None
    assert result.shortfall_factor is None


def test_sigma_is_sample_std_of_pooled_returns() -> None:
    # Given
    returns = (-80., -60., -40., -20., -10., 0., 5., 15., 25., 35., 55., 90.)
    # When
    result = assess_selection_power(
        per_trade_returns_bps=returns, held_by_symbol={"X.US": 3}, delta_bps=20,
    )
    # Then
    assert result.sigma_bps == statistics.stdev(returns)
    assert result.sigma_observations == 12


def test_reach_gate_alpha_and_power_at_floor_six() -> None:
    # Given / When
    result = reach_gate_operating_point(
        n=6, min_rate_pct=60, loser_reach_p=0.22, winner_reach_p=0.85,
    )
    # Then
    assert result.n == 6
    assert result.k_min == 4
    assert result.alpha_at_loser == pytest.approx(0.0239, abs=1e-3)
    assert result.power_at_winner == pytest.approx(0.95266140625, abs=1e-12)


def test_rejects_nonfinite_or_nonpositive_inputs() -> None:
    # Given
    for sigma, delta, alpha, power in (
        (54, 0, .05, .8), (54, -1, .05, .8),
        (0, 20, .05, .8), (-1, 20, .05, .8),
        (54, 20, 0, .8), (54, 20, 1, .8),
        (54, 20, -.1, .8), (54, 20, 1.1, .8),
        (54, 20, .05, 0), (54, 20, .05, 1),
        (math.nan, 20, .05, .8), (54, math.nan, .05, .8),
        (54, 20, math.nan, .8), (54, 20, .05, math.nan),
        (math.inf, 20, .05, .8), (54, math.inf, .05, .8),
    ):
        for calculate in (required_trades_one_sample, required_trades_two_sample):
            # When / Then
            with pytest.raises(ValueError):
                calculate(sigma_bps=sigma, delta_bps=delta, alpha=alpha, power=power)
