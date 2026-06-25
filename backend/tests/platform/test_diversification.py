"""Tests for P242 portfolio diversification diagnostics."""

from __future__ import annotations

import math

import pytest

from app.platform.diversification import (
    concentration_curve,
    concentration_index,
    diversification_benefit,
    diversification_ratio,
    diversification_report,
    effective_n,
)


def test_effective_n_identity_cov_equal_weights():
    # Identity covariance, equal weights -> each asset independent, N_eff = n.
    n = 5
    weights = [1.0 / n] * n
    cov = [[1.0 if i == j else 0.0 for j in range(n)] for i in range(n)]
    ne = effective_n(weights, cov)
    assert abs(ne - n) < 1e-9


def test_effective_n_single_asset_is_one():
    # One asset, any covariance -> N_eff = 1.
    weights = [1.0]
    cov = [[4.0]]
    ne = effective_n(weights, cov)
    assert abs(ne - 1.0) < 1e-9


def test_effective_n_perfectly_correlated_is_one():
    # Perfectly correlated equal-vol assets -> N_eff = 1 (no diversification).
    n = 4
    weights = [1.0 / n] * n
    # All vol = 1, all correlation = 1 -> cov all ones.
    cov = [[1.0 for _ in range(n)] for _ in range(n)]
    ne = effective_n(weights, cov)
    assert abs(ne - 1.0) < 1e-9


def test_effective_n_bounded_by_n():
    # N_eff <= n always for long-only.
    n = 3
    weights = [0.5, 0.3, 0.2]
    cov = [[0.04, 0.01, 0.0], [0.01, 0.09, 0.02], [0.0, 0.02, 0.01]]
    ne = effective_n(weights, cov)
    assert 1.0 - 1e-9 <= ne <= n + 1e-9


def test_diversification_ratio_equals_sqrt_effective_n():
    n = 3
    weights = [0.4, 0.35, 0.25]
    cov = [[0.09, 0.02, 0.01], [0.02, 0.04, 0.0], [0.01, 0.0, 0.01]]
    sigmas = [math.sqrt(cov[i][i]) for i in range(n)]
    dr = diversification_ratio(weights, sigmas, cov)
    ne = effective_n(weights, cov)
    assert abs(dr * dr - ne) < 1e-9
    assert dr >= 1.0 - 1e-12


def test_diversification_ratio_uncorrelated_equal_vol_equals_sqrt_n():
    n = 4
    weights = [1.0 / n] * n
    cov = [[0.25 if i == j else 0.0 for j in range(n)] for i in range(n)]
    sigmas = [0.5] * n
    dr = diversification_ratio(weights, sigmas, cov)
    assert abs(dr - math.sqrt(n)) < 1e-9


def test_diversification_ratio_shape_mismatch():
    with pytest.raises(ValueError):
        diversification_ratio([0.5, 0.5], [1.0, 1.0, 1.0], [[1.0, 0.0], [0.0, 1.0]])


def test_diversification_ratio_empty():
    with pytest.raises(ValueError):
        diversification_ratio([], [], [])


def test_diversification_ratio_non_positive_portfolio_vol():
    # Zero covariance -> portfolio variance zero -> ValueError.
    weights = [0.5, 0.5]
    cov = [[0.0, 0.0], [0.0, 0.0]]
    sigmas = [0.0, 0.0]
    with pytest.raises(ValueError):
        diversification_ratio(weights, sigmas, cov)


def test_diversification_ratio_negative_sigma():
    with pytest.raises(ValueError):
        diversification_ratio([0.5, 0.5], [-1.0, 1.0], [[1.0, 0.0], [0.0, 1.0]])


def test_effective_n_non_square_cov():
    with pytest.raises(ValueError):
        effective_n([0.5, 0.5], [[1.0, 0.0]])  # 1 row, should be 2


def test_effective_n_empty():
    with pytest.raises(ValueError):
        effective_n([], [])


def test_diversification_benefit_zero_when_perfectly_correlated():
    n = 3
    weights = [1.0 / n] * n
    cov = [[1.0 for _ in range(n)] for _ in range(n)]
    sigmas = [1.0] * n
    b = diversification_benefit(weights, sigmas, cov)
    assert abs(b) < 1e-9


def test_diversification_benefit_in_range():
    n = 4
    weights = [0.25, 0.25, 0.25, 0.25]
    cov = [[0.04, 0.0, 0.0, 0.0], [0.0, 0.04, 0.0, 0.0],
           [0.0, 0.0, 0.04, 0.0], [0.0, 0.0, 0.0, 0.04]]
    sigmas = [0.2] * n
    b = diversification_benefit(weights, sigmas, cov)
    # uncorrelated equal-vol: benefit = 1 - sqrt(n*0.25^2*0.04)/(n*0.25*0.2)
    # = 1 - 1/sqrt(n)
    assert abs(b - (1.0 - 1.0 / math.sqrt(n))) < 1e-9
    assert 0.0 - 1e-9 <= b < 1.0


def test_diversification_benefit_weighted_vol_zero():
    # All sigmas zero -> weighted vol zero -> ValueError.
    weights = [0.5, 0.5]
    cov = [[1.0, 0.0], [0.0, 1.0]]
    sigmas = [0.0, 0.0]
    with pytest.raises(ValueError):
        diversification_benefit(weights, sigmas, cov)


def test_concentration_curve_equal_weights():
    weights = [0.25, 0.25, 0.25, 0.25]
    curve = concentration_curve(weights)
    assert len(curve) == 4
    assert abs(curve[-1] - 1.0) < 1e-12
    # Equal weights -> linear rise 0.25, 0.5, 0.75, 1.0
    for i, expected in enumerate([0.25, 0.5, 0.75, 1.0]):
        assert abs(curve[i] - expected) < 1e-9


def test_concentration_curve_concentrated():
    weights = [0.97, 0.01, 0.01, 0.01]
    curve = concentration_curve(weights)
    # Sorted ascending: 0.01, 0.01, 0.01, 0.97 -> cumulative 0.01, 0.02, 0.03, 1.0
    assert abs(curve[0] - 0.01) < 1e-9
    assert abs(curve[2] - 0.03) < 1e-9
    assert abs(curve[-1] - 1.0) < 1e-12


def test_concentration_curve_handles_negative_weights():
    # Mixed-sign book: normalization by absolute sum.
    weights = [0.5, -0.3, 0.2]
    curve = concentration_curve(weights)
    assert abs(curve[-1] - 1.0) < 1e-12
    # Sorted abs: 0.2/1.0=0.2, 0.3/1.0=0.3, 0.5/1.0=0.5 -> 0.2, 0.5, 1.0
    assert abs(curve[0] - 0.2) < 1e-9
    assert abs(curve[1] - 0.5) < 1e-9


def test_concentration_curve_empty():
    with pytest.raises(ValueError):
        concentration_curve([])


def test_concentration_curve_all_zero():
    with pytest.raises(ValueError):
        concentration_curve([0.0, 0.0, 0.0])


def test_concentration_index_equal_weights():
    n = 4
    weights = [1.0 / n] * n
    hhi = concentration_index(weights)
    assert abs(hhi - 1.0 / n) < 1e-9


def test_concentration_index_single_asset():
    weights = [1.0]
    hhi = concentration_index(weights)
    assert abs(hhi - 1.0) < 1e-9


def test_concentration_index_concentrated_higher_than_equal():
    equal = concentration_index([0.25, 0.25, 0.25, 0.25])
    concentrated = concentration_index([0.7, 0.1, 0.1, 0.1])
    assert concentrated > equal


def test_concentration_index_empty():
    with pytest.raises(ValueError):
        concentration_index([])


def test_diversification_report_aggregates():
    n = 3
    weights = [0.4, 0.35, 0.25]
    cov = [[0.09, 0.02, 0.01], [0.02, 0.04, 0.0], [0.01, 0.0, 0.01]]
    sigmas = [math.sqrt(cov[i][i]) for i in range(n)]
    rep = diversification_report(weights, sigmas, cov)
    d = rep.to_dict()
    assert d["n_assets"] == 3
    assert "effective_n" in d
    assert "diversification_ratio" in d
    assert "diversification_benefit" in d
    assert "concentration_index" in d
    # Cross-check consistency.
    assert abs(d["diversification_ratio"] ** 2 - d["effective_n"]) < 1e-9
    assert d["diversification_ratio"] >= 1.0 - 1e-12
    assert 0.0 - 1e-9 <= d["diversification_benefit"] < 1.0


def test_diversification_report_shape_mismatch():
    with pytest.raises(ValueError):
        diversification_report([0.5, 0.5], [1.0], [[1.0, 0.0], [0.0, 1.0]])


def test_diversification_report_empty():
    with pytest.raises(ValueError):
        diversification_report([], [], [])


def test_effective_n_relationship_to_dr_for_uncorrelated():
    # For uncorrelated assets, N_eff = (sum w_i sigma_i)^2 / sum (w_i^2 sigma_i^2)
    n = 3
    weights = [0.5, 0.3, 0.2]
    sigmas = [0.2, 0.3, 0.4]
    cov = [[sigmas[i] ** 2 if i == j else 0.0 for j in range(n)] for i in range(n)]
    ne = effective_n(weights, cov)
    num = sum(w * s for w, s in zip(weights, sigmas)) ** 2
    den = sum((w * s) ** 2 for w, s in zip(weights, sigmas))
    assert abs(ne - num / den) < 1e-9