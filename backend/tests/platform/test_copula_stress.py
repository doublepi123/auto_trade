"""Tests for P378 copula stress module."""

from __future__ import annotations

import pytest

from app.platform.copula_stress import CopulaStressResult, copula_stress_report


def _generate_panel(
    n_assets: int = 2, n_periods: int = 200, seed: int = 42
) -> dict[str, list[float]]:
    """Generate correlated return series for a panel of assets."""
    rng = __import__("random")
    rng.seed(seed)
    # Generate a common factor
    factor = [rng.gauss(0, 0.02) for _ in range(n_periods)]
    panel: dict[str, list[float]] = {}
    for i in range(n_assets):
        # Each asset = 0.7 * factor + 0.3 * idiosyncratic noise
        panel[f"ASSET_{i}"] = [
            0.7 * f + 0.3 * rng.gauss(0, 0.02) + 0.0001 for f in factor
        ]
    return panel


def test_copula_stress_returns_result():
    panel = _generate_panel(n_assets=2, n_periods=200)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=50, seed=42)
    assert isinstance(result, CopulaStressResult)


def test_scenarios_non_empty():
    panel = _generate_panel(n_assets=2, n_periods=200)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=100, seed=42)
    assert len(result.scenarios) == 100
    for scenario in result.scenarios:
        assert isinstance(scenario, dict)
        for asset_name in panel:
            assert asset_name in scenario
            assert isinstance(scenario[asset_name], float)


def test_tail_correlation_in_range():
    panel = _generate_panel(n_assets=2, n_periods=300)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=100, seed=42)
    assert -1.0 <= result.tail_correlation <= 1.0


def test_systemic_loss_length():
    panel = _generate_panel(n_assets=3, n_periods=200)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=100, seed=42)
    assert len(result.systemic_loss) == 100
    for loss in result.systemic_loss:
        assert isinstance(loss, float)


def test_worst_scenario_index_valid():
    panel = _generate_panel(n_assets=2, n_periods=200)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=50, seed=42)
    assert 0 <= result.worst_scenario_index < 50
    # The worst scenario should have the largest systemic loss
    worst_loss = result.systemic_loss[result.worst_scenario_index]
    assert worst_loss == max(result.systemic_loss)


def test_to_dict_roundtrip():
    panel = _generate_panel(n_assets=2, n_periods=100)
    result = copula_stress_report(panel, quantile=0.05, n_scenarios=30, seed=42)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "scenarios" in d
    assert "tail_correlation" in d
    assert "systemic_loss" in d
    assert "worst_scenario_index" in d


def test_validation_errors():
    """Test that invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        copula_stress_report({})
    with pytest.raises(ValueError):
        copula_stress_report({"A": [1.0], "B": [1.0, 2.0]})
    with pytest.raises(ValueError):
        copula_stress_report({"A": [1.0]})
    with pytest.raises(ValueError):
        copula_stress_report({"A": [1.0, 2.0], "B": [1.0, 2.0]}, quantile=0.0)
    with pytest.raises(ValueError):
        copula_stress_report({"A": [1.0, 2.0], "B": [1.0, 2.0]}, quantile=1.0)
    # Panel size limit (>50 assets)
    large_panel = {f"A{i}": [0.01, -0.01] for i in range(51)}
    with pytest.raises(ValueError):
        copula_stress_report(large_panel)
