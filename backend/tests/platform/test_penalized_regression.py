"""Tests for P350 penalized regression (Ridge / Lasso)."""

from __future__ import annotations

import math

import pytest

from app.platform.penalized_regression import (
    PenalizedRegressionResult,
    penalized_regression_report,
)


def test_ridge_slope_approx_2():
    """y = 2*x + noise: ridge slope should be approx 2."""
    n = 50
    x_vals = [float(i) for i in range(n)]
    y = [2.0 * xi + 0.5 for xi in x_vals]
    # add small noise
    y = [yi + 0.01 * (i % 5 - 2) for i, yi in enumerate(y)]
    result = penalized_regression_report(
        y=y,
        x=[x_vals],
        method="ridge",
        alpha=0.01,
    )
    assert isinstance(result, PenalizedRegressionResult)
    assert result.method == "ridge"
    coeff = result.coefficients.get("b1")
    assert coeff is not None
    assert abs(coeff - 2.0) < 0.1
    assert result.r_squared > 0.95
    assert result.residual_std > 0.0


def test_ridge_with_intercept():
    """y = 3 + 1.5*x: ridge recovers both."""
    n = 40
    x_vals = [float(i) for i in range(n)]
    y = [3.0 + 1.5 * xi for xi in x_vals]
    result = penalized_regression_report(
        y=y,
        x=[x_vals],
        method="ridge",
        alpha=0.0,
    )
    assert abs(result.coefficients.get("intercept", 0.0) - 3.0) < 0.01
    coeff = result.coefficients.get("b1")
    assert coeff is not None
    assert abs(coeff - 1.5) < 0.01
    assert result.r_squared > 0.99


def test_ridge_r_squared_range():
    """R-squared should be in [0, 1]."""
    n = 30
    x_vals = [float(i) for i in range(n)]
    y = [2.0 * xi + 1.0 for xi in x_vals]
    result = penalized_regression_report(y=y, x=[x_vals], method="ridge", alpha=0.0)
    assert 0.0 <= result.r_squared <= 1.0


def test_lasso_slope_approx_2():
    """Lasso with y = 2*x: should recover slope ~2."""
    n = 50
    x_vals = [float(i) for i in range(n)]
    y = [2.0 * xi + 0.5 for xi in x_vals]
    result = penalized_regression_report(
        y=y,
        x=[x_vals],
        method="lasso",
        alpha=0.01,
        max_iter=200,
    )
    assert result.method == "lasso"
    coeff = result.coefficients.get("b1")
    assert coeff is not None
    assert abs(coeff - 2.0) < 0.15
    assert result.r_squared > 0.95


def test_lasso_zero_coefficient_for_irrelevant():
    """Irrelevant regressor should get zero or near-zero coefficient under lasso."""
    n = 40
    x1 = [float(i) for i in range(n)]
    x2 = [0.01 * (i % 3) for i in range(n)]  # irrelevant
    y = [3.0 * xi1 + 1.0 for xi1 in x1]
    result = penalized_regression_report(
        y=y,
        x=[x1, x2],
        method="lasso",
        alpha=1.0,
        max_iter=500,
    )
    coeff_b1 = result.coefficients.get("b1")
    coeff_b2 = result.coefficients.get("b2")
    assert coeff_b1 is not None and abs(coeff_b1 - 3.0) < 0.5
    # b2 should be very small (irrelevant)
    assert coeff_b2 is not None and abs(coeff_b2) < 0.5


def test_invalid_method():
    with pytest.raises(ValueError):
        penalized_regression_report(y=[1.0, 2.0], x=[[1.0, 2.0]], method="elastic_net")


def test_empty_y():
    with pytest.raises(ValueError):
        penalized_regression_report(y=[], x=[[]], method="ridge")


def test_length_mismatch():
    with pytest.raises(ValueError):
        penalized_regression_report(y=[1.0, 2.0, 3.0], x=[[1.0, 2.0]], method="ridge")


def test_non_finite_y():
    with pytest.raises(ValueError):
        penalized_regression_report(y=[float("inf"), 1.0], x=[[1.0, 2.0]], method="ridge")


def test_result_to_dict():
    result = penalized_regression_report(
        y=[1.0, 2.0, 3.0, 4.0],
        x=[[0.0, 1.0, 2.0, 3.0]],
        method="ridge",
        alpha=0.0,
    )
    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["method"] == "ridge"
    assert isinstance(d["coefficients"], dict)
    assert isinstance(d["r_squared"], float)
    assert isinstance(d["residual_std"], float)
