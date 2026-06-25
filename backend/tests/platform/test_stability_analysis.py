"""Tests for P222 walk-forward parameter stability diagnostics."""

from __future__ import annotations

import pytest

from app.platform.stability_analysis import (
    analyze_stability,
    degradation_ratio,
    neighborhood_stability,
    optimal_param_drift,
)


def test_degradation_ratio_identity():
    assert degradation_ratio(2.0, 2.0) == 1.0
    assert abs(degradation_ratio(2.0, 1.0) - 0.5) < 1e-9
    assert degradation_ratio(2.0, 8.0) == 4.0  # clipped to cap
    assert degradation_ratio(0.0, 1.0) == 1.0  # floor guard


def test_degradation_ratio_lower_is_better():
    # drawdown-style: IS=-0.1 OOS=-0.2 (worse OOS) → ratio = IS/OOS = 0.5
    r = degradation_ratio(-0.1, -0.2, higher_is_better=False)
    assert abs(r - 0.5) < 1e-9


def test_analyze_stability_perfect_generalization():
    wf = [
        {"params": {"a": 1}, "in_sample_sharpe": 1.5, "out_of_sample_sharpe": 1.4},
        {"params": {"a": 2}, "in_sample_sharpe": 1.3, "out_of_sample_sharpe": 1.2},
        {"params": {"a": 3}, "in_sample_sharpe": 1.1, "out_of_sample_sharpe": 1.0},
    ]
    r = analyze_stability(wf)
    assert r["windows"] == 1
    assert r["degradation"]["mean_ratio"] >= 0.9
    assert r["degradation"]["overfit_flag"] is False


def test_analyze_stability_catastrophic_window():
    wf = [
        {"params": {"a": 1}, "in_sample_sharpe": 2.0, "out_of_sample_sharpe": -0.5},
        {"params": {"a": 2}, "in_sample_sharpe": 1.0, "out_of_sample_sharpe": -1.0},
    ]
    r = analyze_stability(wf)
    # negative OOS → ratio clipped to 0
    assert r["degradation"]["min_ratio"] == 0.0
    assert r["degradation"]["overfit_flag"] is True


def test_neighborhood_stability_single_axis():
    matrix = {(1,): 1.0, (2,): 1.0, (3,): 1.0}
    res = neighborhood_stability(matrix, ["a"], [[1, 2, 3]])
    assert res["a"] == 1.0  # constant → stable


def test_neighborhood_stability_varies():
    matrix = {(1,): 1.0, (2,): 5.0, (3,): 1.0}
    res = neighborhood_stability(matrix, ["a"], [[1, 2, 3]])
    assert res["a"] < 1.0  # neighbor swings → less stable


def test_neighborhood_stability_two_axes():
    matrix = {(1, 10): 1.0, (1, 20): 5.0, (2, 10): 1.0, (2, 20): 5.0}
    res = neighborhood_stability(matrix, ["a", "b"], [[1, 2], [10, 20]])
    # 'a' constant along neighbors, 'b' varies → a more stable than b
    assert res["a"] >= res["b"]


def test_optimal_param_drift_numeric():
    per_window = [{"a": 1}, {"a": 2}, {"a": 1}, {"a": 2}]
    drift = optimal_param_drift(per_window, ["a"])
    assert drift["unique_optima_count"] == 2
    assert 0.0 <= drift["per_axis"]["a"] <= 1.0


def test_optimal_param_drift_categorical():
    per_window = [{"regime": "x"}, {"regime": "x"}, {"regime": "y"}]
    drift = optimal_param_drift(per_window, ["regime"])
    # 1 of 3 differs from modal 'x' → drift 1/3
    assert abs(drift["per_axis"]["regime"] - 1 / 3) < 1e-9


def test_analyze_stability_empty():
    r = analyze_stability([])
    assert r["windows"] == 0
    assert r["degradation"] == {}
    assert r["drift"] == {}


def test_analyze_stability_skips_none_oos():
    wf = [
        {"params": {"a": 1}, "in_sample_sharpe": 1.5, "out_of_sample_sharpe": None},
        {"params": {"a": 2}, "in_sample_sharpe": 1.3, "out_of_sample_sharpe": None},
    ]
    r = analyze_stability(wf)
    assert r["windows"] == 1
    # degradation ratios empty (no OOS) but no exception
    assert r["degradation"]["overfit_flag"] is False


def test_analyze_stability_lower_is_better():
    wf = [
        {"params": {"a": 1}, "in_sample_sharpe": -0.1, "out_of_sample_sharpe": -0.3},
        {"params": {"a": 2}, "in_sample_sharpe": -0.2, "out_of_sample_sharpe": -0.6},
    ]
    r = analyze_stability(wf, higher_is_better=False)
    assert r["higher_is_better"] is False
    # OOS worse than IS (degradation ratio 0.33) → overfit
    assert r["degradation"]["mean_ratio"] < 0.5
    assert r["degradation"]["overfit_flag"] is True


def test_analyze_stability_determinism():
    wf = [
        {"params": {"a": 1}, "in_sample_sharpe": 1.5, "out_of_sample_sharpe": 1.4},
        {"params": {"a": 2}, "in_sample_sharpe": 1.3, "out_of_sample_sharpe": 1.2},
    ]
    a = analyze_stability(wf)
    b = analyze_stability(wf)
    assert a == b