"""Integration tests for P207 Black-Litterman with views, robustness, and protocol wiring."""

from __future__ import annotations

from decimal import Decimal

from app.platform.black_litterman import (
    BlackLittermanModel,
    View,
    black_litterman,
    market_implied_returns,
)
from app.platform.construction import PortfolioConstructionModel


def _cov2(sa: float, sb: float, rho: float) -> dict[tuple[str, str], float]:
    return {
        ("A", "A"): sa * sa, ("B", "B"): sb * sb,
        ("A", "B"): rho * sa * sb, ("B", "A"): rho * sa * sb,
    }


def test_market_implied_returns_zero_for_zero_market_weights():
    cov = _cov2(0.2, 0.1, 0.0)
    pi = market_implied_returns({"A": 0.5, "B": 0.5}, cov, risk_aversion=2.5)
    assert abs(pi["A"] - 2.5 * 0.04 * 0.5) < 1e-9
    assert abs(pi["B"] - 2.5 * 0.01 * 0.5) < 1e-9


def test_black_litterman_no_views_collapses_to_prior():
    cov = _cov2(0.2, 0.1, 0.0)
    prior = {"A": 0.08, "B": 0.04}
    post_r, post_cov = black_litterman(prior, cov, views=[])
    # No views: posterior returns equal the prior.
    assert abs(post_r["A"] - 0.08) < 1e-9
    assert abs(post_r["B"] - 0.04) < 1e-9
    # Covariance is scaled by (1+tau) where tau=0.05 by default.
    assert abs(post_cov[("A", "A")] - 0.04 * 1.05) < 1e-9


def test_absolute_view_raises_expected_return():
    cov = _cov2(0.2, 0.1, 0.0)
    prior = {"A": 0.08, "B": 0.04}
    view = View(assets={"A": 1.0}, expected_return=0.20, confidence=0.9)
    post_r, _ = black_litterman(prior, cov, views=[view])
    # An absolute view that A = 20% should push the posterior of A above prior.
    assert post_r["A"] > prior["A"]


def test_relative_view_favors_advantaged_asset():
    cov = _cov2(0.2, 0.1, 0.0)
    prior = {"A": 0.05, "B": 0.05}
    # View: A outperforms B by 5%.
    view = View(assets={"A": 1.0, "B": -1.0}, expected_return=0.05, confidence=1.0)
    post_r, _ = black_litterman(prior, cov, views=[view])
    # Posterior spread should reflect the view
    assert post_r["A"] > post_r["B"]


def test_confidence_scaling_high_confidence_pulls_toward_view():
    cov = _cov2(0.2, 0.1, 0.0)
    prior = {"A": 0.05, "B": 0.05}
    view_low = View(assets={"A": 1.0}, expected_return=0.15, confidence=0.1)
    view_high = View(assets={"A": 1.0}, expected_return=0.15, confidence=0.99)
    r_low, _ = black_litterman(prior, cov, views=[view_low])
    r_high, _ = black_litterman(prior, cov, views=[view_high])
    # Higher confidence → posterior closer to view → higher expected return on A.
    assert r_high["A"] > r_low["A"]


def test_black_litterman_model_is_construction_model():
    cov = _cov2(0.2, 0.1, 0.0)
    model = BlackLittermanModel(
        market_weights={"A": 0.5, "B": 0.5},
        cov=cov,
        views=[View(assets={"A": 1.0}, expected_return=0.12, confidence=0.8)],
    )
    assert isinstance(model, PortfolioConstructionModel)
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1")})
    # Weights sum to 1
    assert abs(float(sum(w.values())) - 1.0) < 1e-6
    # View tilts toward A → w[A] > w[B]
    assert w["A"] > w["B"]


def test_black_litterman_handles_singular_via_prior_fallback():
    # When the BL inner matrix is singular, the implementation falls back to
    # the prior with a (1+tau) covariance scaling. Test that the function
    # still returns a well-formed posterior without raising.
    cov = {("A", "A"): 0.04, ("B", "B"): 0.04, ("A", "B"): 0.04, ("B", "A"): 0.04}
    prior = {"A": 0.05, "B": 0.05}
    # Multiple views on the same asset to provoke an ill-conditioned inner matrix
    post_r, post_cov = black_litterman(
        prior,
        cov,
        views=[
            View({"A": 1.0}, 0.20, 0.9),
            View({"A": 1.0}, 0.25, 0.9),
            View({"A": 1.0}, 0.30, 0.9),
        ],
    )
    # Whatever the path (full BL or prior fallback), the function should
    # return a well-formed posterior for all symbols.
    assert set(post_r.keys()) == {"A", "B"}
    for i in ("A", "B"):
        for j in ("A", "B"):
            assert (i, j) in post_cov
