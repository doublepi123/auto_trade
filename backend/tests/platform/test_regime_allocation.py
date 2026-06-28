"""P342: Regime allocation tests."""

from __future__ import annotations

import math

import pytest


def test_regime_allocation_basic():
    """2 regimes, 3 assets → recommended_weights non-empty and sum ≈ 1."""
    from app.platform.regime_allocation import regime_allocation_report

    returns_panel = {
        "A": [0.02, 0.03, -0.01, 0.01, -0.005, 0.01, 0.015, -0.01],
        "B": [0.03, 0.05, -0.02, 0.015, -0.01, 0.02, 0.025, -0.015],
        "C": [-0.01, -0.015, 0.01, -0.005, 0.015, -0.02, 0.01, 0.005],
    }
    regimes = ["bull", "bull", "bear", "bull", "bear", "bull", "bull", "bear"]
    result = regime_allocation_report(returns_panel, regimes, current_regime="bull")
    d = result.to_dict()
    assert d["current_regime"] == "bull"
    assert "recommended_weights" in d
    assert len(d["recommended_weights"]) == 3
    weight_sum = sum(d["recommended_weights"].values())
    assert math.isclose(weight_sum, 1.0, rel_tol=1e-9)
    assert "regime_stats" in d
    assert "bull" in d["regime_stats"]
    assert "bear" in d["regime_stats"]
    assert "regime_label" in d
    assert d["regime_label"] in ("bull", "bear", "neutral")


def test_regime_allocation_bear():
    """Bear regime allocation."""
    from app.platform.regime_allocation import regime_allocation_report

    returns_panel = {
        "X": [-0.02, -0.01, -0.03, 0.01, -0.005, -0.01],
        "Y": [-0.01, -0.02, -0.04, 0.02, -0.01, -0.005],
        "Z": [0.01, 0.005, -0.01, 0.0, 0.02, 0.015],
    }
    regimes = ["bear", "bear", "bear", "bull", "bull", "bull"]
    result = regime_allocation_report(returns_panel, regimes, current_regime="bear")
    d = result.to_dict()
    assert d["current_regime"] == "bear"
    assert len(d["recommended_weights"]) == 3
    weight_sum = sum(d["recommended_weights"].values())
    assert math.isclose(weight_sum, 1.0, rel_tol=1e-9)


def test_regime_allocation_rejects_empty_panel():
    from app.platform.regime_allocation import regime_allocation_report

    with pytest.raises(ValueError):
        regime_allocation_report({}, ["bull"], current_regime="bull")


def test_regime_allocation_rejects_mismatched_length():
    from app.platform.regime_allocation import regime_allocation_report

    with pytest.raises(ValueError):
        regime_allocation_report({"A": [0.01, 0.02]}, ["bull"], current_regime="bull")


def test_regime_allocation_rejects_unknown_current_regime():
    from app.platform.regime_allocation import regime_allocation_report

    with pytest.raises(ValueError):
        regime_allocation_report(
            {"A": [0.01, 0.02]}, ["bull", "bear"], current_regime="unknown"
        )


def test_regime_allocation_rejects_nan():
    from app.platform.regime_allocation import regime_allocation_report

    with pytest.raises(ValueError):
        regime_allocation_report(
            {"A": [0.01, float("nan")]}, ["bull", "bear"], current_regime="bull"
        )
