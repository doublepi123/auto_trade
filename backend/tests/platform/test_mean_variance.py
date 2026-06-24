"""Tests for P204 mean-variance optimization + efficient frontier."""

from __future__ import annotations

import math
from decimal import Decimal

from app.platform.mean_variance import (
    MeanVarianceModel,
    efficient_frontier,
    max_sharpe_weights,
    min_variance_weights,
)


def _cov(sigma_a: float, sigma_b: float, rho: float) -> dict[tuple[str, str], float]:
    return {
        ("A", "A"): sigma_a ** 2,
        ("B", "B"): sigma_b ** 2,
        ("A", "B"): rho * sigma_a * sigma_b,
        ("B", "A"): rho * sigma_a * sigma_b,
    }


def test_min_variance_weights_sum_to_one_and_positive():
    cov = _cov(0.2, 0.1, 0.3)
    w = min_variance_weights(cov=cov)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v > 0 for v in w.values())


def test_min_variance_weights_prefer_lower_variance_asset():
    # B is half as volatile as A → min-var should overweight B.
    cov = _cov(0.2, 0.1, 0.0)  # uncorrelated
    w = min_variance_weights(cov=cov)
    assert w["B"] > w["A"]


def test_min_variance_single_asset():
    w = min_variance_weights(cov={("A", "A"): 0.04})
    assert w == {"A": 1.0}


def test_min_variance_from_returns_panel():
    returns = {
        "A": [0.01, -0.01, 0.02, -0.02, 0.0],
        "B": [0.005, -0.005, 0.01, -0.01, 0.0],  # half the vol of A
    }
    w = min_variance_weights(returns=returns)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["B"] >= w["A"]  # lower-vol asset gets at least as much


def test_max_sharpe_weights_concentrate_on_higher_sharpe_asset():
    # A: mu=0.10, vol=0.20 → Sharpe 0.5; B: mu=0.08, vol=0.10 → Sharpe 0.8.
    cov = _cov(0.20, 0.10, 0.0)
    mu = {"A": 0.10, "B": 0.08}
    w = max_sharpe_weights(mu, cov)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert w["B"] > w["A"]  # higher-Sharpe asset overweighted


def test_max_sharpe_single_asset():
    w = max_sharpe_weights({"A": 0.1}, {("A", "A"): 0.04})
    assert w == {"A": 1.0}


def test_efficient_frontier_sorted_by_return():
    mu = {"A": 0.05, "B": 0.10, "C": 0.15}
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16,
        ("A", "B"): 0.0, ("A", "C"): 0.0, ("B", "C"): 0.0,
        ("B", "A"): 0.0, ("C", "A"): 0.0, ("C", "B"): 0.0,
    }
    pts = efficient_frontier(mu, cov, n_points=10)
    rets = [p["return"] for p in pts]
    assert rets == sorted(rets)
    assert len(pts) == 10
    for p in pts:
        assert abs(sum(p["weights"].values()) - 1.0) < 1e-6


def test_efficient_frontier_volatility_nondecreasing_after_min_var():
    mu = {"A": 0.02, "B": 0.12}
    cov = {("A", "A"): 0.01, ("B", "B"): 0.04, ("A", "B"): 0.0, ("B", "A"): 0.0}
    pts = efficient_frontier(mu, cov, n_points=20)
    # The leftmost (lowest-return) points should have low vol; volatility rises
    # toward the high-return end. We assert the last point is more volatile than
    # the first (a coarse monotonicity check tolerant to grid granularity).
    assert pts[-1]["volatility"] >= pts[0]["volatility"] - 1e-6


def test_efficient_frontier_equal_returns_collapses():
    mu = {"A": 0.05, "B": 0.05}
    cov = _cov(0.2, 0.1, 0.0)
    pts = efficient_frontier(mu, cov, n_points=10)
    assert len(pts) == 1
    assert abs(sum(pts[0]["weights"].values()) - 1.0) < 1e-9


def test_mean_variance_model_implements_protocol():
    model = MeanVarianceModel(mean_returns={"A": 0.1, "B": 0.05}, risk_free=0.0)
    signals = {"A": Decimal("1"), "B": Decimal("1")}
    vols = {"A": Decimal("0.2"), "B": Decimal("0.1")}
    w = model.target_weights(signals, volatilities=vols)
    assert set(w.keys()) == {"A", "B"}
    total = float(sum(w.values()))
    assert abs(total - 1.0) < 1e-6


def test_mean_variance_model_empty_signals():
    model = MeanVarianceModel()
    assert model.target_weights({}) == {}


def test_mean_variance_model_name():
    assert MeanVarianceModel().name == "mean_variance"
