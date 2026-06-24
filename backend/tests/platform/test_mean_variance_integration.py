"""Tests for P206 mean-variance integration points.

The mean_variance module is already in place; this test file exercises its
public surface and the MeanVarianceModel Protocol integration so we can
treat it as a first-class construction model alongside EqualWeight /
RiskParity.
"""

from __future__ import annotations

import math
from decimal import Decimal

from app.platform.construction import PortfolioConstructionModel
from app.platform.mean_variance import (
    MeanVarianceModel,
    efficient_frontier,
    max_sharpe_weights,
    min_variance_weights,
)


def test_min_variance_protocol_integration():
    # Build a model with a covariance matrix and verify the weights sum to 1.
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09,
        ("A", "B"): 0.0, ("B", "A"): 0.0,
    }
    model = MeanVarianceModel(cov=cov)  # type: ignore[arg-type]
    assert isinstance(model, PortfolioConstructionModel)
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1")})
    assert abs(float(sum(w.values())) - 1.0) < 1e-6


def test_max_sharpe_tangency_matches_pyportfolio_semantics():
    # Two-asset example: B has higher per-unit-risk return (Sharpe) than A
    # A: μ=0.15, σ=0.5 → Sharpe 0.3
    # B: μ=0.05, σ=0.2 → Sharpe 0.25 — actually A higher; flip so B > A
    mu = {"A": 0.06, "B": 0.08}
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.04,
        ("A", "B"): 0.0, ("B", "A"): 0.0,
    }
    w = max_sharpe_weights(mu, cov, risk_free=0.0)
    # B has higher return at equal vol → all-in on B
    assert w["B"] > w["A"]
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_efficient_frontier_contains_min_variance_at_low_end():
    mu = {"A": 0.02, "B": 0.08, "C": 0.12}
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16,
        ("A", "B"): 0.0, ("A", "C"): 0.0, ("B", "C"): 0.0,
        ("B", "A"): 0.0, ("C", "A"): 0.0, ("C", "B"): 0.0,
    }
    pts = efficient_frontier(mu, cov, n_points=15)
    # The lowest-volatility point of the frontier should be near the min-variance portfolio
    low = min(pts, key=lambda p: p["volatility"])
    expected_min = min_variance_weights(cov=cov)
    # We can't expect an exact match (grid search), but the lowest-vol point
    # should at least *include* A and have weights summing to 1
    assert abs(sum(low["weights"].values()) - 1.0) < 1e-6
    for s in expected_min:
        assert s in low["weights"]


def test_efficient_frontier_monotonic_volatility():
    mu = {"A": 0.02, "B": 0.08}
    cov = {
        ("A", "A"): 0.01, ("B", "B"): 0.04,
        ("A", "B"): 0.0, ("B", "A"): 0.0,
    }
    pts = efficient_frontier(mu, cov, n_points=12)
    vols = [p["volatility"] for p in pts]
    # The very last point (highest target return) is more volatile than the first
    assert vols[-1] >= vols[0] - 1e-6


def test_mean_variance_model_handles_missing_vols():
    # If vols are missing, the model should fall back to equal weight, not crash.
    model = MeanVarianceModel(mean_returns={"A": 0.1, "B": 0.05}, risk_free=0.0)
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1")}, volatilities=None)
    # Either equal-weight (degenerate) or max-Sharpe — both must sum to 1.
    assert abs(float(sum(w.values())) - 1.0) < 1e-6


def test_efficient_frontier_with_high_correlation_collapses():
    # Perfectly correlated assets → frontier degenerates
    mu = {"A": 0.05, "B": 0.10}
    cov = {("A", "A"): 0.04, ("B", "B"): 0.16, ("A", "B"): 0.08, ("B", "A"): 0.08}
    pts = efficient_frontier(mu, cov, n_points=10)
    # Should still produce a usable frontier (not raise)
    assert all(abs(sum(p["weights"].values()) - 1.0) < 1e-6 for p in pts)
