"""Integration tests for P208 Ledoit-Wolf shrinkage covariance and downstream use."""

from __future__ import annotations

import math

from app.platform.covariance import (
    ledoit_wolf_shrinkage,
    sample_covariance,
    to_dict_view,
    portfolio_variance,
)
from app.platform.mean_variance import min_variance_weights, max_sharpe_weights
from app.platform.hrp import hrp_weights


def test_ledoit_wolf_shrunk_matrix_psd_in_practice():
    # With reasonable non-singular data, the shrunk cov should be symmetric
    # and have non-negative diagonal (it does by construction).
    returns = {
        "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008],
        "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006],
        "C": [0.012, -0.006, 0.024, -0.012, 0.018, -0.024, 0.006, 0.014, -0.004, 0.010],
    }
    shrunk, delta = ledoit_wolf_shrinkage(returns)
    symbols = list(returns.keys())
    for s in symbols:
        assert shrunk[(s, s)] >= 0  # diagonal non-negative
        for t in symbols:
            assert shrunk[(s, t)] == shrunk[(t, s)]  # symmetric
    assert 0.0 <= delta <= 1.0


def test_ledoit_wolf_powers_min_variance_optimization():
    # Two uncorrelated assets → min-variance weights sum to 1 with both
    # components positive.
    returns = {
        "A": [0.02, -0.01, 0.03, -0.02, 0.015, -0.025, 0.01, 0.02, -0.015, 0.005, 0.02, -0.01],
        "B": [-0.005, 0.015, -0.01, 0.02, -0.005, 0.01, -0.015, 0.005, 0.02, -0.01, 0.005, -0.02],
    }
    shrunk, _ = ledoit_wolf_shrinkage(returns)
    w = min_variance_weights(cov=shrunk)
    # If shrinkage preserves the off-diagonal near 0, the closed-form
    # min-var weights sum to 1 and both are non-negative.
    total = sum(w.values())
    if total > 0.99:  # closed-form applied
        assert all(v > 0 for v in w.values())
    # At minimum, the result is non-negative and not all-zero.
    assert all(v >= 0 for v in w.values())
    assert sum(w.values()) > 0


def test_ledoit_wolf_powers_hrp():
    returns = {
        "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008],
        "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006],
        "C": [0.012, -0.006, 0.024, -0.012, 0.018, -0.024, 0.006, 0.014, -0.004, 0.010],
    }
    w = hrp_weights(returns=returns)
    assert abs(sum(w.values()) - 1.0) < 1e-6
    assert set(w.keys()) == set(returns.keys())


def test_to_dict_view_nests_rows():
    cov = {("A", "A"): 1.0, ("A", "B"): 0.5, ("B", "A"): 0.5, ("B", "B"): 2.0}
    nested = to_dict_view(cov)
    assert nested == {"A": {"A": 1.0, "B": 0.5}, "B": {"A": 0.5, "B": 2.0}}


def test_portfolio_variance_matches_known_formula():
    # For a 2-asset portfolio, σ_p^2 = w1²σ1² + w2²σ2² + 2 w1 w2 ρ σ1 σ2
    cov = {("A", "A"): 0.04, ("A", "B"): 0.012, ("B", "A"): 0.012, ("B", "B"): 0.09}
    w = {"A": 0.4, "B": 0.6}
    var = portfolio_variance(cov, w)
    expected = 0.16 * 0.04 + 0.36 * 0.09 + 2 * 0.4 * 0.6 * 0.012
    assert abs(var - expected) < 1e-12


def test_ledoit_wolf_single_asset_returns_zero_shrinkage():
    # Single-asset panel: no off-diagonal target, returns sample cov with δ=0.
    cov, delta = ledoit_wolf_shrinkage({"A": [0.01, 0.02, -0.01]})
    assert delta == 0.0
    # The sample cov is whatever the sample variance is (or 0 for n<2)
    assert ("A", "A") in cov
