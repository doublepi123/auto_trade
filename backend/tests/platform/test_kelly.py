"""Tests for P224 Kelly criterion & bet-sizing."""

from __future__ import annotations

import math

import pytest

from app.platform.kelly import (
    expected_log_growth,
    fractional_kelly,
    kelly_binary,
    kelly_from_returns,
    risk_of_ruin,
)


def test_kelly_binary_fair_game_no_edge():
    # p=0.5, even payoff → f*=0
    assert abs(kelly_binary(0.5, 1.0, 1.0)) < 1e-12


def test_kelly_binary_positive_edge():
    # p=0.55, b=1 → f* = (0.55-0.45)/1 = 0.1
    assert abs(kelly_binary(0.55, 1.0, 1.0) - 0.1) < 1e-9


def test_kelly_binary_negative_edge_returns_negative():
    # p=0.4, b=1 → f* = (0.4-0.6)/1 = -0.2 → fade
    assert kelly_binary(0.4, 1.0, 1.0) < 0


def test_kelly_binary_bad_inputs():
    with pytest.raises(ValueError):
        kelly_binary(0.5, 0.0, 1.0)
    with pytest.raises(ValueError):
        kelly_binary(0.5, 1.0, 0.0)
    with pytest.raises(ValueError):
        kelly_binary(1.5, 1.0, 1.0)


def test_kelly_from_returns_positive_edge():
    # Mean positive, some variance → positive Kelly fraction
    rs = [0.01, -0.02, 0.03, 0.0, 0.02, -0.01, 0.04, 0.01]
    f = kelly_from_returns(rs)
    assert 0.0 < f <= 1.0


def test_kelly_from_returns_negative_edge_zero():
    rs = [-0.01, -0.02, -0.03, -0.005, -0.02, -0.01, -0.025, -0.015]
    f = kelly_from_returns(rs)
    assert f == 0.0  # no edge → clamp to 0


def test_kelly_from_returns_too_short():
    with pytest.raises(ValueError):
        kelly_from_returns([0.01])


def test_expected_log_growth_fair_game():
    g = expected_log_growth(0.0, 0.5, 1.0, 1.0)
    assert abs(g) < 1e-12  # no bet → no growth


def test_expected_log_growth_at_full_kelly_maximizes():
    # For p=0.55, b=1, the Kelly f*=0.1; log-growth peaks near 0.1
    g_at_kelly = expected_log_growth(0.1, 0.55, 1.0, 1.0)
    g_above = expected_log_growth(0.2, 0.55, 1.0, 1.0)
    g_below = expected_log_growth(0.05, 0.55, 1.0, 1.0)
    assert g_at_kelly > g_above
    assert g_at_kelly > g_below


def test_expected_log_growth_invalid_fraction():
    with pytest.raises(ValueError):
        expected_log_growth(1.5, 0.5, 1.0, 1.0)


def test_fractional_kelly_report():
    rep = fractional_kelly(0.55, 1.0, 1.0)
    assert rep.has_edge
    assert abs(rep.full_kelly - 0.1) < 1e-9
    assert abs(rep.half_kelly - 0.05) < 1e-9
    assert abs(rep.quarter_kelly - 0.025) < 1e-9
    assert rep.expected_log_growth_full > rep.expected_log_growth_half or rep.expected_log_growth_full == rep.expected_log_growth_half
    d = rep.to_dict()
    assert "full_kelly" in d and "has_edge" in d


def test_fractional_kelly_no_edge():
    rep = fractional_kelly(0.5, 1.0, 1.0)
    assert not rep.has_edge
    assert rep.full_kelly == 0.0


def test_risk_of_ruin_positive_edge_low():
    # Strong edge, small per-bet loss fraction → ruin prob near 0
    p = risk_of_ruin(0.6, 0.2, 0.1, bankroll_units=10.0)
    assert 0.0 <= p < 0.5


def test_risk_of_ruin_zero_bankroll_raises():
    with pytest.raises(ValueError):
        risk_of_ruin(0.55, 1.0, 0.5, bankroll_units=0.0)


def test_risk_of_ruin_fair_game_one():
    # Fair game → no positive edge → ruin prob → 1
    p = risk_of_ruin(0.5, 0.2, 0.2, bankroll_units=5.0)
    assert p == 1.0