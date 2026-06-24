"""Tests for P205 Black-Litterman."""

from __future__ import annotations

from decimal import Decimal

from app.platform.black_litterman import (
    BlackLittermanModel,
    View,
    black_litterman,
    market_implied_returns,
)


def _cov2(va: float, vb: float, rho: float = 0.3) -> dict[tuple[str, str], float]:
    return {
        ("A", "A"): va ** 2, ("B", "B"): vb ** 2,
        ("A", "B"): rho * va * vb, ("B", "A"): rho * va * vb,
    }


def test_market_implied_returns_positive_for_positive_weights():
    cov = _cov2(0.2, 0.1)
    pi = market_implied_returns({"A": 0.5, "B": 0.5}, cov, risk_aversion=2.5)
    # Both assets positive expected excess return under positive weights/cov.
    assert pi["A"] > 0
    assert pi["B"] > 0
    # Higher-vol asset A has higher implied return (Σ·w scaled).
    assert pi["A"] > pi["B"]


def test_no_views_returns_prior_and_scaled_cov():
    prior = {"A": 0.05, "B": 0.03}
    cov = _cov2(0.2, 0.1)
    posterior, post_cov = black_litterman(prior, cov, views=[], tau=0.05)
    assert posterior == prior
    # cov scaled by (1 + tau) when no views
    assert abs(post_cov[("A", "A")] - (1.05) * cov[("A", "A")]) < 1e-12


def test_bullish_view_tilts_posterior_upward():
    prior = {"A": 0.05, "B": 0.05}
    cov = _cov2(0.2, 0.2, rho=0.0)
    views = [View(assets={"A": 1.0}, expected_return=0.15, confidence=1.0)]
    posterior, _ = black_litterman(prior, cov, views, tau=0.05)
    # A bullish confident view on A pulls its posterior above the prior.
    assert posterior["A"] > prior["A"]
    # B is untouched by an A-only absolute view under uncorrelated cov.
    assert abs(posterior["B"] - prior["B"]) < 1e-9


def test_relative_view_tilts_relative_returns():
    prior = {"A": 0.05, "B": 0.05}
    cov = _cov2(0.2, 0.2, rho=0.0)
    # View: A outperforms B by 0.10
    views = [View(assets={"A": 1.0, "B": -1.0}, expected_return=0.10, confidence=1.0)]
    posterior, _ = black_litterman(prior, cov, views, tau=0.05)
    spread = posterior["A"] - posterior["B"]
    assert spread > 0
    assert abs(spread - 0.10) < 1e-6  # confident view realized exactly


def test_low_confidence_view_has_smaller_effect():
    prior = {"A": 0.05, "B": 0.05}
    cov = _cov2(0.2, 0.2, rho=0.0)
    high = black_litterman(prior, cov, [View({"A": 1.0}, 0.20, confidence=0.99)], tau=0.05)
    low = black_litterman(prior, cov, [View({"A": 1.0}, 0.20, confidence=0.1)], tau=0.05)
    # More confident view moves posterior further from prior.
    assert high[0]["A"] - prior["A"] > low[0]["A"] - prior["A"]


def test_posterior_cov_diagonal_decreases_or_equals_with_certain_view():
    prior = {"A": 0.05, "B": 0.05}
    cov = _cov2(0.2, 0.2, rho=0.0)
    views = [View(assets={"A": 1.0}, expected_return=0.15, confidence=1.0)]
    _, post_cov = black_litterman(prior, cov, views, tau=0.05)
    # A fully-certain view reduces A's posterior variance vs the no-view scaled cov.
    no_view_cov = black_litterman(prior, cov, [], tau=0.05)[1]
    assert post_cov[("A", "A")] <= no_view_cov[("A", "A")] + 1e-12


def test_black_litterman_model_weights_concentrate_on_viewed_asset():
    cov = _cov2(0.2, 0.2, rho=0.0)
    prior = market_implied_returns({"A": 0.5, "B": 0.5}, cov)
    model = BlackLittermanModel(
        market_weights={"A": 0.5, "B": 0.5},
        cov=cov,
        views=[View(assets={"A": 1.0}, expected_return=prior["A"] + 0.10, confidence=1.0)],
    )
    signals = {"A": Decimal("1"), "B": Decimal("1")}
    w = model.target_weights(signals)
    assert abs(float(sum(w.values())) - 1.0) < 1e-6
    # Bullish view on A → overweight A vs the equal-market prior.
    assert w["A"] > w["B"]


def test_black_litterman_model_empty_signals():
    model = BlackLittermanModel(
        market_weights={"A": 0.5, "B": 0.5},
        cov=_cov2(0.2, 0.2),
        views=[],
    )
    assert model.target_weights({}) == {}


def test_black_litterman_model_name():
    model = BlackLittermanModel(
        market_weights={"A": 1.0}, cov={("A", "A"): 0.04}, views=[]
    )
    assert model.name == "black_litterman"
