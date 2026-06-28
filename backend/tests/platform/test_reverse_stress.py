"""Tests for P332 reverse stress testing module."""

from __future__ import annotations

import pytest

from app.platform.reverse_stress import ReverseStressResult, reverse_stress_report


def test_reverse_stress_default_scenarios_returns_result():
    positions = {"AAPL": 10000.0, "GOOG": 5000.0}
    betas = {"AAPL": 1.2, "GOOG": 1.0}
    result = reverse_stress_report(positions, betas, loss_threshold=2000.0)
    assert isinstance(result, ReverseStressResult)
    assert result.critical_multiplier > 0
    assert len(result.scenario_details) > 0
    assert result.critical_scenario_name != ""


def test_reverse_stress_positive_multiplier():
    positions = {"AAPL": 10000.0}
    betas = {"AAPL": 1.0}
    result = reverse_stress_report(positions, betas, loss_threshold=500.0)
    assert result.critical_multiplier > 0
    # With market_return = -0.10 (equity_crash), portfolio_loss = 10000 * 1.0 * (-0.10) = -1000
    # loss_threshold / abs(portfolio_loss) = 500/1000 = 0.5
    assert result.critical_multiplier == 0.5


def test_reverse_stress_custom_scenarios():
    positions = {"A": 5000.0}
    betas = {"A": 2.0}
    custom = [
        {"name": "mild", "market_return": -0.02},
        {"name": "severe", "market_return": -0.15},
    ]
    result = reverse_stress_report(positions, betas, loss_threshold=1000.0, scenarios=custom)
    # severe: loss = 5000*2.0*(-0.15) = -1500, multiplier = 1000/1500 = 0.666...
    assert result.critical_scenario_name == "severe"
    assert abs(result.critical_multiplier - 1000.0 / 1500.0) < 1e-9


def test_reverse_stress_multiplier_greater_than_one_when_safe():
    positions = {"A": 1000.0}
    betas = {"A": 0.5}
    # equity_crash (-0.10): loss = 1000*0.5*0.10 = 50, multiplier = 500/50 = 10
    result = reverse_stress_report(positions, betas, loss_threshold=500.0)
    assert result.critical_multiplier > 1.0


def test_reverse_stress_scenario_details_count():
    positions = {"A": 5000.0}
    betas = {"A": 1.0}
    result = reverse_stress_report(positions, betas, loss_threshold=1000.0)
    assert len(result.scenario_details) == 3  # default: equity_crash, vol_spike, corr_breakdown
    names = [d["name"] for d in result.scenario_details]
    assert "equity_crash" in names
    assert "vol_spike" in names
    assert "corr_breakdown" in names


def test_reverse_stress_invalid_loss_threshold_raises():
    positions = {"A": 1000.0}
    betas = {"A": 1.0}
    try:
        reverse_stress_report(positions, betas, loss_threshold=0.0)
        assert False, "expected ValueError"
    except ValueError:
        pass


def test_reverse_stress_to_dict():
    positions = {"A": 5000.0}
    betas = {"A": 1.0}
    result = reverse_stress_report(positions, betas, loss_threshold=1000.0)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "critical_scenario_name" in d
    assert "critical_multiplier" in d
    assert "scenario_details" in d
    assert isinstance(d["scenario_details"], list)


def test_reverse_stress_rejects_non_dict_scenario():
    from app.platform.reverse_stress import reverse_stress_report
    with pytest.raises(ValueError):
        reverse_stress_report({"A": 1000.0}, {"A": 1.0}, 100.0, scenarios=[123])  # type: ignore[list-item]


def test_reverse_stress_rejects_scenario_missing_market_return():
    from app.platform.reverse_stress import reverse_stress_report
    with pytest.raises(ValueError):
        reverse_stress_report({"A": 1000.0}, {"A": 1.0}, 100.0, scenarios=[{"name": "s1"}])
