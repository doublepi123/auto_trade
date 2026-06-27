"""Tests for P312 liquidity_adjusted_returns module."""

from __future__ import annotations

import pytest

from app.platform.liquidity_adjusted_returns import liquidity_adjusted_returns_report


def test_amihud_adjusted_returns_lower_than_raw():
    """Amihud adjustment should reduce returns when volume is finite."""
    returns = [0.01, -0.005, 0.02, -0.01, 0.005]
    volumes = [1000.0, 2000.0, 1500.0, 3000.0, 2500.0]
    result = liquidity_adjusted_returns_report(returns, volumes, method="amihud")
    d = result.to_dict()

    assert len(d["raw_returns"]) == 5
    assert len(d["adjusted_returns"]) == 5
    assert d["illiquidity_metric"] > 0

    # Each adjusted return should be ≤ raw return (illiquidity penalty subtracts)
    for raw, adj in zip(d["raw_returns"], d["adjusted_returns"]):
        assert adj <= raw, f"adjusted {adj} > raw {raw}"


def test_roll_adjusted_returns_lower_than_raw():
    """Roll spread adjustment should reduce returns."""
    returns = [0.01, -0.005, 0.02, -0.01, 0.005]
    volumes = [1000.0, 2000.0, 1500.0, 3000.0, 2500.0]
    result = liquidity_adjusted_returns_report(returns, volumes, method="roll")
    d = result.to_dict()

    assert len(d["adjusted_returns"]) == 5
    assert d["illiquidity_metric"] >= 0

    for raw, adj in zip(d["raw_returns"], d["adjusted_returns"]):
        assert adj <= raw, f"adjusted {adj} > raw {raw}"


def test_amihud_illiquidity_zero_when_volume_huge():
    """When volumes are huge, Amihud illiquidity approaches 0."""
    returns = [0.01, -0.005, 0.02]
    volumes = [1e12, 1e12, 1e12]
    result = liquidity_adjusted_returns_report(returns, volumes, method="amihud")
    d = result.to_dict()
    # Illiquidity should be very small, adjusted ≈ raw
    assert d["illiquidity_metric"] < 1e-8
    for raw, adj in zip(d["raw_returns"], d["adjusted_returns"]):
        assert abs(raw - adj) < 1e-8


def test_liquidity_adjusted_rejects_empty():
    with pytest.raises(ValueError):
        liquidity_adjusted_returns_report([], [], method="amihud")


def test_liquidity_adjusted_rejects_mismatched_lengths():
    with pytest.raises(ValueError):
        liquidity_adjusted_returns_report([0.01, 0.02], [1000.0], method="amihud")


def test_liquidity_adjusted_rejects_invalid_method():
    with pytest.raises(ValueError):
        liquidity_adjusted_returns_report([0.01], [1000.0], method="invalid")


def test_liquidity_adjusted_rejects_zero_volume():
    with pytest.raises(ValueError):
        liquidity_adjusted_returns_report([0.01], [0.0], method="amihud")


def test_roll_spread_non_negative():
    """Roll spread must be ≥ 0 (sqrt ensures this)."""
    returns = [0.01, 0.02, 0.015, 0.005, -0.01, 0.03, 0.02, 0.01]
    volumes = [1000.0] * 8
    result = liquidity_adjusted_returns_report(returns, volumes, method="roll")
    assert result.illiquidity_metric >= 0
