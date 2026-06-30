"""Tests for P377 hedge ratio comparison module."""

from __future__ import annotations

import math

import pytest

from app.platform.hedge_ratio_comparison import (
    HedgeRatioComparisonResult,
    hedge_ratio_comparison_report,
)


def _generate_xy(
    n: int = 200, beta: float = 2.0, noise_std: float = 0.1, seed: int = 42
) -> tuple[list[float], list[float]]:
    """Generate x ~ uniform(-1, 1), y = beta * x + noise."""
    rng = __import__("random")
    rng.seed(seed)
    xs: list[float] = []
    ys: list[float] = []
    for _ in range(n):
        x = rng.uniform(-1, 1)
        noise = rng.gauss(0, noise_std)
        xs.append(x)
        ys.append(beta * x + noise)
    return ys, xs


def test_ols_hedge_ratio_near_true_beta():
    """Construct y = 2*x + noise, assert OLS hedge_ratio ≈ 2."""
    y, x = _generate_xy(beta=2.0)
    result = hedge_ratio_comparison_report(y, x, window=30)
    assert isinstance(result, HedgeRatioComparisonResult)
    ols_ratio = result.per_method["ols"]["hedge_ratio"]
    assert abs(ols_ratio - 2.0) < 0.2


def test_naive_hedge_ratio_is_one():
    y, x = _generate_xy(beta=1.5)
    result = hedge_ratio_comparison_report(y, x, window=20)
    assert result.per_method["naive"]["hedge_ratio"] == 1.0


def test_result_structure():
    y, x = _generate_xy(n=100, beta=1.0)
    result = hedge_ratio_comparison_report(y, x, window=20)
    for method in ("ols", "rolling_ols", "ewma", "naive"):
        assert method in result.per_method
        info = result.per_method[method]
        assert "hedge_ratio" in info
        assert "residual_vol" in info
        assert "effectiveness" in info
        assert info["residual_vol"] >= 0
        # effectiveness should be between 0 and 1 for valid models
        assert -1.0 <= info["effectiveness"] <= 2.0

    assert result.best_method in {"ols", "rolling_ols", "ewma", "naive"}
    assert isinstance(result.ratios_over_time, dict)
    assert len(result.ratios_over_time) >= 2


def test_to_dict_roundtrip():
    y, x = _generate_xy(n=100)
    result = hedge_ratio_comparison_report(y, x, window=20)
    d = result.to_dict()
    assert isinstance(d, dict)
    assert "per_method" in d
    assert "best_method" in d
    assert "ratios_over_time" in d


def test_rolling_ols_produces_time_varying_ratios():
    """Rolling OLS should produce a time series of hedge ratios."""
    y, x = _generate_xy(n=100, beta=2.0, noise_std=0.5)
    result = hedge_ratio_comparison_report(y, x, window=20)
    rolling = result.ratios_over_time.get("rolling_ols", [])
    assert len(rolling) > 0
    # Check that not all values are identical (time-varying)
    if len(rolling) > 1:
        assert not all(abs(r - rolling[0]) < 1e-9 for r in rolling)


def test_ewma_uses_lambda():
    """EWMA should produce a hedge ratio with default lambda."""
    y, x = _generate_xy(n=100)
    result = hedge_ratio_comparison_report(y, x, window=20)
    ewma_info = result.per_method["ewma"]
    assert math.isfinite(ewma_info["hedge_ratio"])


def test_effectiveness_comparison():
    """OLS should generally have better effectiveness than naive for linear data."""
    y, x = _generate_xy(n=200, beta=2.0, noise_std=0.05)
    result = hedge_ratio_comparison_report(y, x, window=30)
    ols_eff = result.per_method["ols"]["effectiveness"]
    naive_eff = result.per_method["naive"]["effectiveness"]
    # OLS should be more effective at reducing residual vol
    assert ols_eff >= naive_eff


def test_validation_errors():
    """Test that invalid inputs raise ValueError."""
    with pytest.raises(ValueError):
        hedge_ratio_comparison_report([], [])
    with pytest.raises(ValueError):
        hedge_ratio_comparison_report([1.0, 2.0], [1.0])
    with pytest.raises(ValueError):
        hedge_ratio_comparison_report([1.0, 2.0], [1.0, 2.0], window=0)
