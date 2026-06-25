"""Tests for P217 convex risk budgeting / ERC."""

from __future__ import annotations

import math
from decimal import Decimal

import pytest

from app.platform.covariance import portfolio_variance
from app.platform.mean_variance import min_variance_weights
from app.platform.risk_budgeting import (
    RiskBudgetingModel,
    risk_budgeting,
    risk_budgeting_converged,
    risk_budgeting_weights,
    risk_contributions,
)


def _cov_diag(va: float, vb: float, cab: float = 0.0) -> dict[tuple[str, str], float]:
    return {("A", "A"): va, ("B", "B"): vb, ("A", "B"): cab, ("B", "A"): cab}


def test_risk_budgeting_empty():
    r = risk_budgeting({})
    assert r["weights"] == {}
    assert r["risk_contributions"] == {}


def test_risk_budgeting_single_asset():
    r = risk_budgeting({("A", "A"): 0.04})
    assert r["weights"] == {"A": 1.0}
    assert r["risk_contributions"] == {"A": 0.04}
    assert r["relative_risk_contributions"] == {"A": 1.0}


def test_risk_budgeting_two_asset_erc_diagonal():
    # diag: A var 0.04 (sigma 0.2), B var 0.01 (sigma 0.1), rho 0 → ERC
    r = risk_budgeting(_cov_diag(0.04, 0.01, 0.0))
    w = r["weights"]
    assert abs(w["A"] - 1.0 / 3.0) < 1e-6
    assert abs(w["B"] - 2.0 / 3.0) < 1e-6
    rc = r["risk_contributions"]
    assert abs(rc["A"] - rc["B"]) < 1e-9
    rel = r["relative_risk_contributions"]
    assert abs(rel["A"] - 0.5) < 1e-6 and abs(rel["B"] - 0.5) < 1e-6


def test_risk_budgeting_two_asset_erc_correlated():
    # rho 0.5: A sigma 0.2, B sigma 0.1 → cross term cancels in equality
    cab = 0.5 * 0.2 * 0.1  # 0.01
    r = risk_budgeting(_cov_diag(0.04, 0.01, cab))
    w = r["weights"]
    assert abs(w["A"] - 1.0 / 3.0) < 1e-6
    assert abs(w["B"] - 2.0 / 3.0) < 1e-6
    rc = r["risk_contributions"]
    assert abs(rc["A"] - rc["B"]) < 1e-8


def test_risk_budgeting_two_asset_budgeted():
    r = risk_budgeting(_cov_diag(0.04, 0.01, 0.0), budgets={"A": 0.25, "B": 0.75})
    rel = r["relative_risk_contributions"]
    assert abs(rel["A"] - 0.25) < 1e-6
    assert abs(rel["B"] - 0.75) < 1e-6


def test_risk_budgeting_three_asset_erc_diagonal():
    # sigmas 0.2/0.3/0.4 rho 0 → ERC weights ∝ 1/sigma
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16,
        ("A", "B"): 0.0, ("B", "A"): 0.0,
        ("A", "C"): 0.0, ("C", "A"): 0.0,
        ("B", "C"): 0.0, ("C", "B"): 0.0,
    }
    r = risk_budgeting(cov)
    w = r["weights"]
    # expected [1/0.2, 1/0.3, 1/0.4] normalized = [0.4615, 0.3077, 0.2308]
    assert abs(w["A"] - 0.461538) < 1e-5
    assert abs(w["B"] - 0.307692) < 1e-5
    assert abs(w["C"] - 0.230769) < 1e-5
    rc = r["risk_contributions"]
    assert abs(rc["A"] - rc["B"]) < 1e-9 and abs(rc["B"] - rc["C"]) < 1e-9


def test_risk_budgeting_three_asset_erc_correlated():
    sig = [0.2, 0.3, 0.4]
    rho = 0.3
    cov = {}
    for i, a in enumerate(["A", "B", "C"]):
        for j, b in enumerate(["A", "B", "C"]):
            if i == j:
                cov[(a, b)] = sig[i] ** 2
            else:
                cov[(a, b)] = rho * sig[i] * sig[j]
    r = risk_budgeting(cov)
    w = r["weights"]
    assert abs(sum(w.values()) - 1.0) < 1e-9
    rel = r["relative_risk_contributions"]
    for s in ["A", "B", "C"]:
        assert abs(rel[s] - 1.0 / 3.0) < 1e-5


def test_risk_budgeting_sum_to_one_and_long_only():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16,
        ("A", "B"): 0.01, ("B", "A"): 0.01,
        ("A", "C"): 0.02, ("C", "A"): 0.02,
        ("B", "C"): 0.03, ("C", "B"): 0.03,
    }
    r = risk_budgeting(cov)
    assert abs(sum(r["weights"].values()) - 1.0) < 1e-9
    assert all(v > 0 for v in r["weights"].values())


def test_risk_budgeting_nan_raises():
    cov = _cov_diag(float("nan"), 0.01)
    with pytest.raises(ValueError):
        risk_budgeting(cov)


def test_risk_budgeting_nonsymmetric_raises():
    cov = {("A", "A"): 0.04, ("B", "B"): 0.01, ("A", "B"): 0.005, ("B", "A"): 0.009}
    with pytest.raises(ValueError):
        risk_budgeting(cov)


def test_risk_budgeting_zero_variance_dropped():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.01, ("C", "C"): 0.0,
        ("A", "B"): 0.005, ("B", "A"): 0.005,
        ("A", "C"): 0.0, ("C", "A"): 0.0,
        ("B", "C"): 0.0, ("C", "B"): 0.0,
    }
    r = risk_budgeting(cov)
    w = r["weights"]
    assert w["C"] == 0.0
    assert abs(sum(w.values()) - 1.0) < 1e-9


def test_risk_budgeting_budgets_unknown_symbol_raises():
    with pytest.raises(ValueError):
        risk_budgeting(_cov_diag(0.04, 0.01), budgets={"Z": 0.5})


def test_risk_budgeting_budgets_renormalized():
    r = risk_budgeting(_cov_diag(0.04, 0.01), budgets={"A": 1.0, "B": 1.0})
    rel = r["relative_risk_contributions"]
    assert abs(rel["A"] - 0.5) < 1e-5 and abs(rel["B"] - 0.5) < 1e-5


def test_risk_contributions_consistency():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("A", "B"): 0.02, ("B", "A"): 0.02,
    }
    w = min_variance_weights(cov=cov)
    rc = risk_contributions(cov, w)
    assert abs(sum(rc.values()) - portfolio_variance(cov, w)) < 1e-12


def test_risk_budgeting_weights_from_returns():
    panel = {
        "A": [0.01, -0.005, 0.02, 0.005, -0.01, 0.015, -0.002, 0.012, 0.003, -0.008,
              0.01, 0.0, -0.002, 0.005, 0.011, -0.004, 0.007, -0.001, 0.009, 0.003],
        "B": [0.005, 0.002, -0.003, 0.008, 0.001, -0.002, 0.006, 0.004, -0.001, 0.007,
              0.0, 0.003, -0.002, 0.005, 0.001, -0.003, 0.008, 0.002, -0.004, 0.006],
        "C": [0.0, 0.001, -0.002, 0.003, 0.0, -0.001, 0.002, 0.0, -0.003, 0.001,
              0.002, -0.001, 0.0, 0.003, -0.002, 0.001, 0.0, -0.002, 0.003, 0.001],
    }
    w = risk_budgeting_weights(returns=panel)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v > 0 for v in w.values())


def test_risk_budgeting_converged_helper():
    rc = {"A": 1.0, "B": 1.0}
    assert risk_budgeting_converged(rc, {"A": 0.5, "B": 0.5}) is True
    assert risk_budgeting_converged(rc, {"A": 0.7, "B": 0.3}) is False


def test_risk_budgeting_model_protocol():
    cov = _cov_diag(0.04, 0.01)
    model = RiskBudgetingModel(cov=cov)
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1")})
    assert all(isinstance(v, Decimal) for v in w.values())
    assert abs(float(sum(w.values())) - 1.0) < 1e-5