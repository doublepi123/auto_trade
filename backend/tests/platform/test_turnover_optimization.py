"""Tests for P216 turnover-aware portfolio optimization."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.platform.covariance import sample_covariance
from app.platform.mean_variance import min_variance_weights
from app.platform.turnover_optimization import (
    TurnoverAwareModel,
    turnover_aware_optimize,
    turnover_objective,
    turnover_penalty,
)


def _cov(va: float, cab: float, vb: float) -> dict[tuple[str, str], float]:
    return {("A", "A"): va, ("A", "B"): cab, ("B", "A"): cab, ("B", "B"): vb}


def _panel() -> dict[str, list[float]]:
    # 2 assets, mild correlation
    return {
        "A": [0.01, -0.005, 0.02, 0.005, -0.01, 0.015, -0.002, 0.012, 0.003, -0.008],
        "B": [0.005, 0.002, -0.003, 0.008, 0.001, -0.002, 0.006, 0.004, -0.001, 0.007],
    }


def test_turnover_sum_to_one_and_positive():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.5, "B": 0.5}
    mu = {"A": 0.10, "B": 0.08}
    w = turnover_aware_optimize(prev, cov, mu, gamma=0.1, lam=1.0)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert all(v >= -1e-9 for v in w.values())


def test_turnover_reduces_churn_vs_plain_min_var():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.5, "B": 0.5}
    plain = min_variance_weights(cov=cov)
    churned = turnover_aware_optimize(prev, cov, None, gamma=2.0, lam=0.0)
    assert turnover_penalty(churned, prev) <= turnover_penalty(plain, prev) + 1e-6


def test_turnover_cap_respected():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.2, "B": 0.8}
    w = turnover_aware_optimize(prev, cov, None, gamma=1.0, delta_cap=0.1, lam=0.0)
    t = turnover_penalty(w, prev)
    assert t <= 0.1 + 1e-5


def test_turnover_gamma_zero_equals_min_var():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.3, "B": 0.7}
    w = turnover_aware_optimize(prev, cov, None, gamma=0.0, lam=0.0, max_iter=2000, tol=1e-9)
    plain = min_variance_weights(cov=cov)
    for s in plain:
        assert abs(w[s] - plain[s]) < 1e-2


def test_turnover_new_symbol_prev_zero():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 1.0}
    w = turnover_aware_optimize(prev, cov, None, gamma=0.1, lam=0.0)
    assert "A" in w and "B" in w
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert w["B"] >= 0.0


def test_turnover_empty_prev_falls_back():
    cov = _cov(0.04, 0.01, 0.02)
    mu = {"A": 0.10, "B": 0.08}
    w = turnover_aware_optimize({}, cov, mu, gamma=0.5, lam=1.0)
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_turnover_single_asset():
    cov = {("A", "A"): 0.04}
    w = turnover_aware_optimize({"A": 1.0}, cov, None, gamma=1.0)
    assert w == {"A": 1.0}


def test_turnover_objective_decreases():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.5, "B": 0.5}
    mu = {"A": 0.10, "B": 0.08}
    init_obj = turnover_objective(prev, cov, mu, prev, gamma=0.5, lam=1.0)
    w = turnover_aware_optimize(prev, cov, mu, gamma=0.5, lam=1.0)
    final_obj = turnover_objective(w, cov, mu, prev, gamma=0.5, lam=1.0)
    assert final_obj <= init_obj + 1e-9


def test_turnover_nan_raises():
    cov = {("A", "A"): float("nan"), ("A", "B"): 0.0, ("B", "A"): 0.0, ("B", "B"): 0.02}
    with pytest.raises(ValueError):
        turnover_aware_optimize({"A": 0.5, "B": 0.5}, cov, None, gamma=0.1)


def test_turnover_deterministic():
    cov = _cov(0.04, 0.01, 0.02)
    prev = {"A": 0.5, "B": 0.5}
    a = turnover_aware_optimize(prev, cov, {"A": 0.1, "B": 0.08}, gamma=0.5)
    b = turnover_aware_optimize(prev, cov, {"A": 0.1, "B": 0.08}, gamma=0.5)
    assert a == b


def test_turnover_degenerate_singular_cov():
    cov = {("A", "A"): 0.0, ("A", "B"): 0.0, ("B", "A"): 0.0, ("B", "B"): 0.0}
    prev = {"A": 0.5, "B": 0.5}
    w = turnover_aware_optimize(prev, cov, {"A": 0.1, "B": 0.08}, gamma=0.1)
    assert abs(sum(w.values()) - 1.0) < 1e-6


def test_turnover_model_protocol():
    model = TurnoverAwareModel(prev_weights={"A": 0.5, "B": 0.5}, gamma=0.5)
    w = model.target_weights(
        {"A": Decimal("0.10"), "B": Decimal("0.08")},
        volatilities={"A": Decimal("0.2"), "B": Decimal("0.1")},
    )
    assert all(isinstance(v, Decimal) for v in w.values())
    assert abs(float(sum(w.values())) - 1.0) < 1e-5