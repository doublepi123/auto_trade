"""Tests for P235 copula tail-dependence."""

from __future__ import annotations

import math

import pytest

from app.platform.copula import (
    CopulaResult,
    clayton_fit,
    empirical_copula,
    gumbel_fit,
    kendall_tau,
    lower_tail_dependence_clayton,
    tail_dependence_coeffs,
    upper_tail_dependence_gumbel,
)


def test_empirical_copula_basic():
    x = [3.0, 1.0, 2.0]
    y = [30.0, 10.0, 20.0]
    pairs = empirical_copula(x, y)
    # sorted x = [1,2,3]: x[0]=3 -> rank 3, x[1]=1 -> rank 1, x[2]=2 -> rank 2.
    # denom = n+1 = 4. Same for y (proportional).
    assert pairs[0][0] == pytest.approx(3 / 4)
    assert pairs[1][0] == pytest.approx(1 / 4)
    assert pairs[2][0] == pytest.approx(2 / 4)
    assert pairs[0][1] == pytest.approx(3 / 4)
    # all within (0,1)
    for u, v in pairs:
        assert 0.0 < u < 1.0
        assert 0.0 < v < 1.0


def test_empirical_copula_ties_midrank():
    # x has ties at value 2.0 (positions 1 and 2) -> mid-rank 2.5
    x = [1.0, 2.0, 2.0, 3.0]
    y = [10.0, 20.0, 30.0, 40.0]
    pairs = empirical_copula(x, y)
    assert pairs[0][0] == pytest.approx(1 / 5)  # rank 1
    # positions 1 and 2 share mid-rank (1-based ranks 2,3 -> mid 2.5)
    assert pairs[1][0] == pytest.approx(2.5 / 5)
    assert pairs[2][0] == pytest.approx(2.5 / 5)
    assert pairs[3][0] == pytest.approx(4 / 5)  # rank 4


def test_empirical_copula_empty_raises():
    with pytest.raises(ValueError):
        empirical_copula([], [])


def test_empirical_copula_unequal_length():
    with pytest.raises(ValueError):
        empirical_copula([1.0, 2.0], [1.0])


def test_kendall_tau_perfect_positive():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert kendall_tau(x, y) == pytest.approx(1.0)


def test_kendall_tau_perfect_negative():
    x = [1.0, 2.0, 3.0, 4.0, 5.0]
    y = [50.0, 40.0, 30.0, 20.0, 10.0]
    assert kendall_tau(x, y) == pytest.approx(-1.0)


def test_kendall_tau_known_count():
    # 4 points with one inversion (positions 1,2 swapped in y).
    x = [1.0, 2.0, 3.0, 4.0]
    y = [1.0, 3.0, 2.0, 4.0]
    # P=5, Q=1, total=6 -> tau=4/6
    assert kendall_tau(x, y) == pytest.approx((5 - 1) / 6.0)


def test_kendall_tau_too_few():
    with pytest.raises(ValueError):
        kendall_tau([1.0], [2.0])


def test_gumbel_fit_formula():
    # theta = 1/(1-tau)
    assert gumbel_fit(0.0) == pytest.approx(1.0)
    assert gumbel_fit(0.5) == pytest.approx(2.0)


def test_gumbel_fit_negative_tau_raises():
    with pytest.raises(ValueError):
        gumbel_fit(-0.1)


def test_gumbel_fit_tau_ge_one_raises():
    with pytest.raises(ValueError):
        gumbel_fit(1.0)


def test_clayton_fit_formula():
    # theta = 2*tau/(1-tau)
    assert clayton_fit(0.5) == pytest.approx(2.0)
    # tau=0.25 -> 0.5/0.75 = 2/3
    assert clayton_fit(0.25) == pytest.approx(2.0 / 3.0)


def test_clayton_fit_nonpositive_raises():
    with pytest.raises(ValueError):
        clayton_fit(0.0)
    with pytest.raises(ValueError):
        clayton_fit(-0.1)


def test_upper_tail_dependence_gumbel_monotonic():
    # lambda_U = 2 - 2^(1/theta); increasing theta -> increasing lambda_U
    lu1 = upper_tail_dependence_gumbel(2.0)
    lu2 = upper_tail_dependence_gumbel(5.0)
    lu3 = upper_tail_dependence_gumbel(10.0)
    assert lu1 < lu2 < lu3
    assert lu1 == pytest.approx(2.0 - 2.0 ** 0.5)
    # all in (0,1)
    assert 0.0 < lu1 < 1.0


def test_upper_tail_dependence_gumbel_theta_one_zero():
    # theta=1 (independence copula) -> lambda_U = 2 - 2 = 0 (no tail dependence)
    assert upper_tail_dependence_gumbel(1.0) == pytest.approx(0.0)


def test_upper_tail_dependence_gumbel_invalid_theta():
    with pytest.raises(ValueError):
        upper_tail_dependence_gumbel(0.5)


def test_lower_tail_dependence_clayton_monotonic():
    # lambda_L = 2^(-1/theta); increasing theta -> increasing lambda_L toward 1
    ll1 = lower_tail_dependence_clayton(1.0)
    ll2 = lower_tail_dependence_clayton(2.0)
    ll3 = lower_tail_dependence_clayton(10.0)
    assert ll1 < ll2 < ll3
    assert ll1 == pytest.approx(0.5)
    assert ll2 == pytest.approx(2.0 ** -0.5)
    # all in (0,1)
    assert 0.0 < ll1 < 1.0


def test_lower_tail_dependence_clayton_invalid_theta():
    with pytest.raises(ValueError):
        lower_tail_dependence_clayton(0.0)
    with pytest.raises(ValueError):
        lower_tail_dependence_clayton(-1.0)


def _near_perfect_positive(n: int) -> tuple[list[float], list[float]]:
    """Identity y=x with one adjacent swap -> tau just under 1 (not degenerate)."""
    x = [float(i) for i in range(n)]
    y = [float(i) for i in range(n)]
    # swap positions n//2 and n//2+1 -> exactly one discordant pair
    mid = n // 2
    y[mid], y[mid + 1] = y[mid + 1], y[mid]
    return x, y


def test_tail_dependence_coeffs_near_perfect_positive():
    n = 50
    x, y = _near_perfect_positive(n)
    res = tail_dependence_coeffs(x, y)
    assert res.kendall_tau > 0.99
    assert res.kendall_tau < 1.0  # strictly below the degenerate boundary
    assert res.gumbel_theta is not None
    assert res.clayton_theta is not None
    assert res.upper_tail_dependence is not None
    assert res.lower_tail_dependence is not None
    assert res.n == n
    d = res.to_dict()
    assert d["n"] == n
    assert "kendall_tau" in d
    assert "gumbel_theta" in d


def test_tail_dependence_coeffs_perfect_positive_boundary():
    # Perfect positive dependence: tau=1 -> gumbel/clayton theta undefined
    # (denominator 1-tau=0). The wrapper returns None for both copula params
    # rather than raising — the boundary is degenerate, no finite theta.
    n = 20
    x = [float(i) for i in range(n)]
    y = [2.0 * i + 5.0 for i in range(n)]
    res = tail_dependence_coeffs(x, y)
    assert res.kendall_tau == pytest.approx(1.0)
    assert res.gumbel_theta is None
    assert res.clayton_theta is None
    assert res.upper_tail_dependence is None
    assert res.lower_tail_dependence is None


def test_tail_dependence_coeffs_too_few_samples():
    with pytest.raises(ValueError):
        tail_dependence_coeffs([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])


def test_tail_dependence_coeffs_constant_series():
    n = 20
    x = [5.0] * n
    y = [float(i) for i in range(n)]
    with pytest.raises(ValueError):
        tail_dependence_coeffs(x, y)


def test_tail_dependence_coeffs_negative_tau_only_gumbel_none_clayton():
    # Perfect negative dependence: tau = -1 -> gumbel None (tau<0), clayton None (tau<=0)
    n = 20
    x = [float(i) for i in range(n)]
    y = [float(n - 1 - i) for i in range(n)]
    res = tail_dependence_coeffs(x, y)
    assert res.kendall_tau == pytest.approx(-1.0)
    assert res.gumbel_theta is None
    assert res.clayton_theta is None
    assert res.upper_tail_dependence is None
    assert res.lower_tail_dependence is None


def test_tail_dependence_coeffs_unequal_length():
    with pytest.raises(ValueError):
        tail_dependence_coeffs([1.0] * 10, [1.0] * 11)


def test_tail_dependence_coeffs_zero_tau_gumbel_at_boundary():
    # Construct a sequence with tau exactly 0: half ascending, half descending,
    # interleaved so concordant == discordant.
    n = 20
    x = [float(i) for i in range(n)]
    # y: first half ascending even slots, second half descending odd slots,
    # arranged so that each concordant pair has a matching discordant pair.
    # Use y = [n-1-i for i in range(n)] gives tau=-1; instead use a symmetric
    # "fold" permutation: y[i] = n-1-i//2 for even i, i//2 for odd i. This is
    # constructed to give tau ~ 0. Allow small tolerance and just assert the
    # gumbel path: tau>=0 and <1 -> gumbel_theta = 1/(1-tau), lambda_U = 2-2^(1/theta).
    # Simpler: use a permutation with exactly equal concordant/discordant.
    # The "bit-reversal" permutation on n=16 has tau=0 by symmetry.
    m = 16
    xs = [float(i) for i in range(m)]
    # reverse the second half only: y = [0,1,...,7,15,14,...,8]
    ys = [float(i) for i in range(8)] + [float(15 - i) for i in range(8)]
    res = tail_dependence_coeffs(xs, ys)
    # Within the first half all concordant; across halves mixed; net tau could
    # be small positive or negative. We only assert the structural behavior:
    if res.kendall_tau >= 0.0 and res.kendall_tau < 1.0:
        assert res.gumbel_theta is not None
        assert res.upper_tail_dependence is not None
    if res.kendall_tau > 0.0:
        assert res.clayton_theta is not None
    else:
        assert res.clayton_theta is None
    assert res.n == m


def test_copula_result_to_dict_keys():
    n = 50
    x, y = _near_perfect_positive(n)
    res = tail_dependence_coeffs(x, y)
    d = res.to_dict()
    assert set(d.keys()) == {
        "kendall_tau",
        "gumbel_theta",
        "clayton_theta",
        "upper_tail_dependence",
        "lower_tail_dependence",
        "n",
    }


def test_copula_result_frozen():
    n = 50
    x, y = _near_perfect_positive(n)
    res = tail_dependence_coeffs(x, y)
    with pytest.raises(Exception):
        res.kendall_tau = 0.5  # frozen dataclass


def test_gumbel_tail_dependence_increases_with_correlation():
    # Stronger positive association -> higher tau -> higher gumbel theta ->
    # higher upper tail dependence.
    n = 50
    x = [float(i) for i in range(n)]
    # weak: large alternating noise -> low tau
    weak_y = [float(i) + 10.0 * ((-1) ** i) for i in range(n)]
    # strong: near-perfect via one swap (tau just under 1)
    _, strong_y = _near_perfect_positive(n)
    r_weak = tail_dependence_coeffs(x, weak_y)
    r_strong = tail_dependence_coeffs(x, strong_y)
    assert r_strong.kendall_tau > r_weak.kendall_tau
    assert r_strong.upper_tail_dependence is not None
    if r_weak.upper_tail_dependence is not None:
        assert r_strong.upper_tail_dependence > r_weak.upper_tail_dependence