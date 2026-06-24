"""Tests for P203 covariance & shrinkage estimation."""

from __future__ import annotations

from app.platform.covariance import (
    covariance_to_correlation,
    ledoit_wolf_shrinkage,
    matrix_from_pairs,
    portfolio_variance,
    sample_covariance,
)


def test_sample_covariance_diagonal_matches_variance():
    # Two series with known variance.
    returns = {
        "A": [0.01, -0.01, 0.02, -0.02, 0.01],
        "B": [0.02, -0.02, 0.04, -0.04, 0.02],
    }
    cov = sample_covariance(returns)
    # B = 2*A exactly, so cov(A,B) = 2*var(A), cov(B,B) = 4*var(A).
    var_a = cov[("A", "A")]
    assert abs(cov[("A", "B")] - 2.0 * var_a) < 1e-12
    assert abs(cov[("B", "B")] - 4.0 * var_a) < 1e-12
    # symmetric
    assert cov[("A", "B")] == cov[("B", "A")]


def test_sample_covariance_perfectly_correlated():
    returns = {
        "A": [1.0, 2.0, 3.0, 4.0],
        "B": [2.0, 4.0, 6.0, 8.0],  # exactly 2x
    }
    cov = sample_covariance(returns)
    corr = covariance_to_correlation(cov, ["A", "B"])
    assert abs(corr[("A", "B")] - 1.0) < 1e-9


def test_sample_covariance_empty_returns_empty():
    assert sample_covariance({}) == {}


def test_sample_covariance_too_few_points_returns_zeros():
    cov = sample_covariance({"A": [0.01], "B": [0.02]})
    assert cov[("A", "A")] == 0.0
    assert cov[("A", "B")] == 0.0


def test_ledoit_wolf_intensity_in_unit_interval():
    returns = {
        "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012],
        "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010],
        "C": [0.02, -0.01, 0.04, -0.02, 0.03, -0.04, 0.01, 0.025],
    }
    _, delta = ledoit_wolf_shrinkage(returns)
    assert 0.0 <= delta <= 1.0


def test_ledoit_wolf_with_tiny_sample_gives_no_shrinkage():
    # n < 2 → no shrinkage, returns sample (zeros) with delta 0.
    cov, delta = ledoit_wolf_shrinkage({"A": [0.01], "B": [0.02]})
    assert delta == 0.0


def test_ledoit_wolf_intensity_is_data_dependent_not_always_one():
    # P203-P212 review fix: the old implementation dropped the ρ̂ cross-term and
    # summed π̂/γ̂ over the diagonal too, which over-estimated δ and clamped it
    # to 1.0 for essentially every input. With the off-diagonal-only π̂/γ̂ and
    # the ρ̂ term included, δ must be a genuine function of the data — a mixed-
    # correlation panel (some correlated, some independent) should produce a
    # δ strictly inside (0, 1), not pinned at 1.0.
    import random

    random.seed(2024)
    n = 200
    z = [random.gauss(0, 1) for _ in range(n)]
    # A,B driven by the same factor (correlated); C,D independent of A,B and each other.
    panel = {
        "A": [0.01 * x + random.gauss(0, 0.005) for x in z],
        "B": [0.01 * x + random.gauss(0, 0.005) for x in z],
        "C": [0.01 * random.gauss(0, 1) + random.gauss(0, 0.005) for _ in range(n)],
        "D": [0.01 * random.gauss(0, 1) + random.gauss(0, 0.005) for _ in range(n)],
    }
    _, delta = ledoit_wolf_shrinkage(panel)
    assert 0.0 < delta < 1.0


def test_ledoit_wolf_shrunk_matrix_between_sample_and_target():
    # With enough noisy data, shrunk cov should be finite and symmetric, and the
    # diagonal close to (but not necessarily equal to) sample variance.
    returns = {
        "A": [0.01, -0.005, 0.02, -0.01, 0.015, -0.02, 0.005, 0.012, -0.003, 0.008],
        "B": [0.008, -0.003, 0.018, -0.008, 0.013, -0.018, 0.003, 0.010, -0.001, 0.006],
    }
    sample = sample_covariance(returns)
    shrunk, delta = ledoit_wolf_shrinkage(returns)
    assert shrunk[("A", "B")] == shrunk[("B", "A")]
    # shrunk diagonal is a blend, so between 0 and sample when delta in (0,1).
    if delta < 1.0:
        assert abs(shrunk[("A", "A")] - sample[("A", "A")]) <= abs(sample[("A", "A")])


def test_matrix_from_pairs_projects_dense():
    cov = {("A", "A"): 1.0, ("A", "B"): 0.5, ("B", "A"): 0.5, ("B", "B"): 2.0}
    dense = matrix_from_pairs(cov, ["A", "B"])
    assert dense == [[1.0, 0.5], [0.5, 2.0]]


def test_portfolio_variance_known_case():
    # Two uncorrelated assets each variance 0.04, weights 0.5/0.5 → 0.02.
    cov = {("A", "A"): 0.04, ("A", "B"): 0.0, ("B", "A"): 0.0, ("B", "B"): 0.04}
    var = portfolio_variance(cov, {"A": 0.5, "B": 0.5})
    assert abs(var - 0.02) < 1e-9


def test_portfolio_variance_with_correlation():
    cov = {("A", "A"): 0.04, ("A", "B"): 0.02, ("B", "A"): 0.02, ("B", "B"): 0.04}
    var = portfolio_variance(cov, {"A": 0.5, "B": 0.5})
    # 0.25*0.04 + 0.25*0.04 + 2*0.25*0.02 = 0.01 + 0.01 + 0.01 = 0.03
    assert abs(var - 0.03) < 1e-9
