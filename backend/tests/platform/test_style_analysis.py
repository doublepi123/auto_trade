"""Tests for P215 Returns-Based Style Analysis (NNLS + simplex)."""

from __future__ import annotations

import math

import pytest

from app.platform.style_analysis import (
    nnls,
    nnls_simplex,
    style_analysis,
)


def test_nnls_known_value():
    A = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    b = [1.0, 0.0, 1.0]
    x = nnls(A, b)
    assert abs(x[0] - 1.0) < 1e-9
    assert abs(x[1]) < 1e-9


def test_style_analysis_recovers_exact_sum_one_exposure():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01]
    r = [0.6 * f1[i] + 0.4 * f2[i] for i in range(8)]
    res = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_eq_one")
    assert abs(res.weights["F1"] - 0.6) < 1e-6
    assert abs(res.weights["F2"] - 0.4) < 1e-6
    assert abs(res.r_squared - 1.0) < 1e-9
    assert abs(res.tracking_error) < 1e-9
    assert abs(res.sum_weights - 1.0) < 1e-9


def test_style_analysis_le_mode_with_cash_residual():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01]
    r = [0.5 * f1[i] for i in range(8)]  # 50% cash
    res = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_le_one")
    assert abs(res.weights["F1"] - 0.5) < 1e-6
    assert abs(res.weights["F2"]) < 1e-6
    assert abs(res.r_squared - 1.0) < 1e-9
    assert abs(res.sum_weights - 0.5) < 1e-6


def test_style_analysis_negative_exposure_clipped():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01]
    r = [1.0 * f1[i] + (-0.5) * f2[i] for i in range(8)]
    res = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_eq_one")
    assert abs(res.weights["F2"]) < 1e-6
    assert abs(res.weights["F1"] - 1.0) < 1e-6
    assert res.r_squared < 0.999  # nonneg constraint binds → imperfect fit


def test_style_analysis_le_falls_back_to_eq_when_sum_exceeds_one():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01]
    # plain NNLS would give x=[1.2, 0], sum 1.2 > 1 → boundary eq solution.
    r = [1.2 * f1[i] for i in range(8)]
    res = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_le_one")
    assert abs(res.sum_weights - 1.0) < 1e-6
    assert abs(res.weights["F1"] - 1.0) < 1e-6
    assert abs(res.weights["F2"]) < 1e-6


def test_style_analysis_none_mode_no_sum_constraint():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    r = [1.2 * f1[i] for i in range(8)]
    res = style_analysis(r, {"F1": f1}, constraint="none")
    assert abs(res.weights["F1"] - 1.2) < 1e-6
    assert abs(res.r_squared - 1.0) < 1e-9


def test_style_analysis_nan_raises():
    with pytest.raises(ValueError):
        style_analysis([0.01, float("nan"), 0.02], {"F1": [0.01, 0.02, 0.03]})


def test_style_analysis_inf_raises():
    with pytest.raises(ValueError):
        style_analysis([0.01, float("inf"), 0.02], {"F1": [0.01, 0.02, 0.03]})


def test_style_analysis_empty_returns_raises():
    with pytest.raises(ValueError):
        style_analysis([], {"F1": [0.01, 0.02]})


def test_style_analysis_empty_factors_raises():
    with pytest.raises(ValueError):
        style_analysis([0.01, 0.02], {})


def test_style_analysis_unknown_constraint_raises():
    with pytest.raises(ValueError):
        style_analysis([0.01, 0.02], {"F1": [0.01, 0.02]}, constraint="banana")


def test_style_analysis_length_mismatch_aligns_to_min():
    f1 = [0.01, 0.02, 0.03, 0.04, 0.05, 0.06, 0.07, 0.08]
    r = [f1[i] for i in range(8)]  # r == f1 exactly
    res = style_analysis(r[:10], {"F1": f1[:8]}, constraint="sum_eq_one")
    assert abs(res.weights["F1"] - 1.0) < 1e-6
    assert 0.0 <= res.r_squared <= 1.0


def test_style_analysis_single_factor_exact():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    r = list(f1)
    res = style_analysis(r, {"F1": f1}, constraint="sum_eq_one")
    assert abs(res.weights["F1"] - 1.0) < 1e-6
    assert abs(res.tracking_error) < 1e-9


def test_style_analysis_collinear_no_raise():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [2.0 * v for v in f1]  # perfectly collinear
    r = list(f1)
    res = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_eq_one")
    assert abs(res.sum_weights - 1.0) < 1e-6 or abs(res.sum_weights) < 1e-6
    assert 0.0 <= res.r_squared <= 1.0


def test_style_analysis_determinism():
    f1 = [0.01, 0.02, -0.01, 0.03, 0.0, -0.02, 0.04, 0.01]
    f2 = [0.0, -0.01, 0.02, 0.01, -0.03, 0.02, 0.0, 0.01]
    r = [0.6 * f1[i] + 0.4 * f2[i] for i in range(8)]
    a = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_eq_one")
    b = style_analysis(r, {"F1": f1, "F2": f2}, constraint="sum_eq_one")
    assert a == b


def test_nnls_simplex_sums_to_one():
    A = [[1.0, 0.0], [0.0, 1.0], [1.0, 1.0]]
    b = [0.6, 0.4, 1.0]
    x = nnls_simplex(A, b)
    assert abs(sum(x) - 1.0) < 1e-6
    assert all(v >= -1e-9 for v in x)