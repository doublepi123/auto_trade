"""P339: CVaR optimization tests."""

from __future__ import annotations

import math

import pytest


def test_cvar_optimization_basic():
    """3 assets → result has optimal_weights (non-empty, sum ≈ 1) and cvar."""
    from app.platform.cvar_optimization import cvar_optimization_report

    returns_panel = {
        "A": [0.01, 0.02, -0.01, 0.005, -0.005, 0.01, -0.02, 0.015, 0.0, 0.005],
        "B": [0.02, 0.04, -0.02, 0.01, -0.01, 0.02, -0.04, 0.03, 0.0, 0.01],
        "C": [-0.005, -0.01, 0.005, -0.005, 0.01, -0.015, 0.02, -0.01, 0.005, 0.0],
    }
    result = cvar_optimization_report(returns_panel, confidence=0.95)
    d = result.to_dict()
    assert "optimal_weights" in d
    assert len(d["optimal_weights"]) == 3
    assert all(w >= 0 for w in d["optimal_weights"].values())
    weight_sum = sum(d["optimal_weights"].values())
    assert math.isclose(weight_sum, 1.0, rel_tol=1e-9)
    assert "cvar" in d
    assert "var" in d
    assert "weights_candidates" in d
    assert isinstance(d["weights_candidates"], list)
    assert len(d["weights_candidates"]) > 0


def test_cvar_optimization_with_target_return():
    """With target_return, result still valid."""
    from app.platform.cvar_optimization import cvar_optimization_report

    returns_panel = {
        "X": [0.01, -0.02, 0.03, -0.01, 0.0, 0.02, -0.01, 0.0, 0.01, -0.005],
        "Y": [0.02, -0.04, 0.06, -0.02, 0.01, 0.04, -0.02, 0.01, 0.02, -0.01],
        "Z": [-0.005, 0.01, -0.02, 0.02, -0.005, 0.01, 0.0, -0.01, 0.005, 0.0],
    }
    result = cvar_optimization_report(returns_panel, confidence=0.90, target_return=0.005)
    d = result.to_dict()
    assert len(d["optimal_weights"]) == 3


def test_cvar_optimization_rejects_empty_panel():
    from app.platform.cvar_optimization import cvar_optimization_report

    with pytest.raises(ValueError):
        cvar_optimization_report({})


def test_cvar_optimization_rejects_non_numeric():
    from app.platform.cvar_optimization import cvar_optimization_report

    with pytest.raises(ValueError):
        cvar_optimization_report({"A": [0.01, float("nan")]})


def test_cvar_optimization_rejects_unequal_length():
    from app.platform.cvar_optimization import cvar_optimization_report

    with pytest.raises(ValueError):
        cvar_optimization_report({"A": [0.01, 0.02], "B": [0.01]})
