"""Tests for P206 Hierarchical Risk Parity."""

from __future__ import annotations

from decimal import Decimal

from app.platform.hrp import HRPModel, correlation_distance, hrp_weights, recursive_bisection


def _cov2(va: float, vb: float, rho: float = 0.3) -> dict[tuple[str, str], float]:
    return {
        ("A", "A"): va ** 2, ("B", "B"): vb ** 2,
        ("A", "B"): rho * va * vb, ("B", "A"): rho * va * vb,
    }


def test_hrp_weights_sum_to_one_and_positive():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16,
        ("A", "B"): 0.02, ("A", "C"): 0.01, ("B", "C"): 0.03,
        ("B", "A"): 0.02, ("C", "A"): 0.01, ("C", "B"): 0.03,
    }
    w = hrp_weights(cov=cov)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v > 0 for v in w.values())


def test_hrp_weights_prefer_lower_variance():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.16,  # A half the vol of B
        ("A", "B"): 0.0, ("B", "A"): 0.0,
    }
    w = hrp_weights(cov=cov)
    assert w["A"] > w["B"]


def test_hrp_identical_assets_get_equal_weight():
    # All variances equal, zero correlation → HRP splits evenly.
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.04, ("C", "C"): 0.04,
        ("A", "B"): 0.0, ("A", "C"): 0.0, ("B", "C"): 0.0,
        ("B", "A"): 0.0, ("C", "A"): 0.0, ("C", "B"): 0.0,
    }
    w = hrp_weights(cov=cov)
    for s in ("A", "B", "C"):
        assert abs(w[s] - 1.0 / 3) < 1e-9


def test_hrp_single_asset():
    w = hrp_weights(cov={("A", "A"): 0.04})
    assert w == {"A": 1.0}


def test_hrp_from_returns_panel():
    returns = {
        "A": [0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.015],
        "B": [0.02, -0.02, 0.04, -0.04, 0.01, -0.01, 0.03],
        "C": [0.005, -0.005, 0.01, -0.01, 0.0025, -0.0025, 0.0075],
    }
    w = hrp_weights(returns=returns)
    assert abs(sum(w.values()) - 1.0) < 1e-9
    # C has lowest vol → gets the most weight.
    assert w["C"] == max(w.values())


def test_correlation_distance_zero_for_perfect_correlation():
    dist = correlation_distance({("A", "A"): 1.0, ("B", "B"): 1.0, ("A", "B"): 1.0, ("B", "A"): 1.0}, ["A", "B"])
    assert dist[("A", "B")] == 0.0


def test_correlation_distance_max_for_negative_correlation():
    dist = correlation_distance({("A", "A"): 1.0, ("B", "B"): 1.0, ("A", "B"): -1.0, ("B", "A"): -1.0}, ["A", "B"])
    assert abs(dist[("A", "B")] - 1.0) < 1e-9


def test_recursive_bisection_preserves_unit_capital():
    cov = {
        ("A", "A"): 0.04, ("B", "B"): 0.09, ("C", "C"): 0.16, ("D", "D"): 0.01,
        ("A", "B"): 0.0, ("A", "C"): 0.0, ("A", "D"): 0.0,
        ("B", "C"): 0.0, ("B", "D"): 0.0, ("C", "D"): 0.0,
        ("B", "A"): 0.0, ("C", "A"): 0.0, ("D", "A"): 0.0,
        ("C", "B"): 0.0, ("D", "B"): 0.0, ("D", "C"): 0.0,
    }
    w = recursive_bisection(["A", "B", "C", "D"], cov)
    # For uncorrelated assets HRP reduces to inverse-variance weighting.
    inv_var = {s: 1.0 / cov[(s, s)] for s in w}
    total = sum(inv_var.values())
    for s in w:
        assert abs(w[s] - inv_var[s] / total) < 1e-9


def test_hrp_model_from_panel_implements_protocol():
    panel = {
        "A": [0.01, -0.01, 0.02, -0.02, 0.005, -0.005, 0.015],
        "B": [0.02, -0.02, 0.04, -0.04, 0.01, -0.01, 0.03],
        "C": [0.005, -0.005, 0.01, -0.01, 0.0025, -0.0025, 0.0075],
    }
    model = HRPModel(returns_panel=panel)
    w = model.target_weights({"A": Decimal("1"), "B": Decimal("1"), "C": Decimal("1")})
    assert set(w.keys()) == {"A", "B", "C"}
    assert abs(float(sum(w.values())) - 1.0) < 1e-6


def test_hrp_model_empty_signals():
    assert HRPModel().target_weights({}) == {}


def test_hrp_model_name():
    assert HRPModel().name == "hrp"
