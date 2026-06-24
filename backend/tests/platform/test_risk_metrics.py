"""Tests for P203 VaR / CVaR risk metrics."""

from __future__ import annotations

import math

from app.platform.risk_metrics import (
    historical_cvar,
    historical_var,
    parametric_cvar,
    parametric_var,
    portfolio_var,
    risk_metrics,
)


def test_historical_var_simple():
    # 1% daily loss should appear in the 99% VaR tail.
    rets = [0.01, -0.02, 0.005, -0.015, 0.02, -0.01, 0.0]
    var95 = historical_var(rets, confidence=0.95)
    assert var95 >= 0.02  # 2% loss


def test_historical_var_uses_loss_convention():
    rets = [-0.05, -0.01, 0.02, 0.03]
    # 95% confidence: the worst loss is 5%
    assert abs(historical_var(rets, confidence=0.95) - 0.05) < 1e-9


def test_historical_var_return_loss_as_negative_flag():
    rets = [-0.10, 0.05, 0.02, 0.01]
    pos = historical_var(rets, confidence=0.75, return_loss_as_negative=False)
    neg = historical_var(rets, confidence=0.75, return_loss_as_negative=True)
    assert pos > 0 and neg < 0 and abs(pos + neg) < 1e-12


def test_historical_cvar_greater_or_equal_to_var():
    rets = [-0.05, -0.02, -0.01, 0.01, 0.02, 0.03, 0.04]
    var = historical_var(rets, 0.95)
    cvar = historical_cvar(rets, 0.95)
    assert cvar >= var


def test_historical_cvar_mean_of_tail():
    rets = [-0.04, -0.03, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03]
    # At 95%, the tail (worst 5%) is [-0.04]; mean loss = 0.04
    assert abs(historical_cvar(rets, 0.95) - 0.04) < 1e-9


def test_parametric_var_zero_mean_known_case():
    # Returns with mean 0, std = 1 → 95% VaR = 1.645 (z-score), 99% = 2.326.
    rets = [-1.0, 0.0, 1.0]
    var = parametric_var(rets, 0.95)
    assert abs(var - 1.6448536) < 1e-4
    var99 = parametric_var(rets, 0.99)
    assert abs(var99 - 2.3263478) < 1e-4


def test_parametric_cvar_higher_than_var():
    # Zero-mean returns with real spread → both VaR and CVaR are loss-positive
    # and CVaR strictly dominates VaR in the tail.
    rets = [-0.10, -0.05, -0.02, -0.01, 0.0, 0.01, 0.02, 0.05, 0.10]
    pv = parametric_var(rets, 0.95)
    pcv = parametric_cvar(rets, 0.95)
    assert pcv >= pv - 1e-9


def test_parametric_var_handles_normal_quantile_edges():
    # High confidence (e.g. 99%) → VaR is a real loss magnitude.
    rets = [0.01, 0.02, -0.01, 0.03, -0.02, 0.0, -0.03]
    assert parametric_var(rets, 0.99) >= 0
    assert parametric_var(rets, 0.90) >= 0


def test_portfolio_var_uses_returns_not_variance():
    a = [0.01, -0.02, 0.03, -0.01]
    b = [0.005, -0.01, 0.015, -0.005]  # perfectly correlated w/ a, half the magnitude
    rets = {"A": a, "B": b}
    # Equal-weight portfolio is 0.75 * a; the worst return ≈ -0.015.
    var = portfolio_var(rets, {"A": 0.5, "B": 0.5}, confidence=0.75)
    assert var >= 0.012  # 0.5 * 0.02 + 0.5 * 0.01 = 0.015


def test_portfolio_var_handles_method_flag():
    a = [0.01, -0.02, 0.03, -0.01, 0.005]
    rets = {"A": a}
    hist = portfolio_var(rets, {"A": 1.0}, confidence=0.80, method="historical")
    para = portfolio_var(rets, {"A": 1.0}, confidence=0.80, method="parametric")
    # Both should be positive loss numbers; may differ but both sensible.
    assert hist >= 0 and para >= 0


def test_risk_metrics_aggregates_at_multiple_confidences():
    rets = [-0.05, -0.02, -0.01, 0.0, 0.01, 0.02, 0.03, 0.04]
    rep = risk_metrics(rets, confidence_levels=[0.90, 0.95, 0.99])
    assert rep["n"] == 8
    assert "90" in rep["var"]["historical"]
    assert "99" in rep["cvar"]["parametric"]
    # Strictly worse tail → CVaR ≥ VaR at every confidence.
    for conf_key in rep["var"]["historical"]:
        assert rep["cvar"]["historical"][conf_key] >= rep["var"]["historical"][conf_key] - 1e-9


def test_risk_metrics_empty_returns():
    rep = risk_metrics([])
    assert rep["n"] == 0
    assert rep["var"] == {}
    assert rep["cvar"] == {}


def test_risk_metrics_default_confidence_levels():
    rets = [-0.02, 0.0, 0.01, 0.02, -0.01]
    rep = risk_metrics(rets)
    for conf_key in ("90", "95", "99"):
        assert conf_key in rep["var"]["historical"]
        assert conf_key in rep["cvar"]["historical"]
