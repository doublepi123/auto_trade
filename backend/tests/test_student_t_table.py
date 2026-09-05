"""Offline table generator/oracle; no numeric solver enters runtime domain code."""
from __future__ import annotations

import math
from statistics import NormalDist

import pytest

from app.domain.watchlist_quant_v6.semantics import SESSION_CLUSTER_T90_BY_DF


def _nonzero(value: float) -> float:
    return value if abs(value) >= 1e-300 else math.copysign(1e-300, value)


def _beta_fraction(a: float, b: float, x: float) -> float:
    """Modified Lentz continued fraction for the incomplete beta function."""
    c = 1.0
    d = 1.0 / _nonzero(1.0 - (a + b) * x / (a + 1.0))
    h = d
    for m in range(1, 10001):
        even = m * (b - m) * x / ((a + 2 * m - 1) * (a + 2 * m))
        odd = -(a + m) * (a + b + m) * x / ((a + 2 * m) * (a + 2 * m + 1))
        for coefficient in (even, odd):
            d = 1.0 / _nonzero(1.0 + coefficient * d)
            c = _nonzero(1.0 + coefficient / c)
            delta = d * c
            h *= delta
        if abs(delta - 1.0) < 3e-14:
            return h
    raise ArithmeticError("incomplete beta continued fraction did not converge")


def _regularized_beta(a: float, b: float, x: float) -> float:
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    factor = math.exp(
        math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
        + a * math.log(x) + b * math.log1p(-x)
    )
    if x < (a + 1.0) / (a + b + 2.0):
        return factor * _beta_fraction(a, b, x) / a
    return 1.0 - factor * _beta_fraction(b, a, 1.0 - x) / b


def student_t_quantile(df: int, probability: float) -> float:
    """Invert F(t)=1-I_{df/(df+t*t)}(df/2,1/2)/2 by bisection."""
    low, high = 0.0, 1.0
    while 1.0 - 0.5 * _regularized_beta(df / 2.0, 0.5, df / (df + high * high)) < probability:
        high *= 2.0
    for _ in range(100):
        middle = (low + high) / 2.0
        cdf = 1.0 - 0.5 * _regularized_beta(df / 2.0, 0.5, df / (df + middle * middle))
        if cdf < probability:
            low = middle
        else:
            high = middle
    return (low + high) / 2.0


def test_generator_matches_all_29_quant_v6_preregistered_values() -> None:
    # Given: an independent, already frozen one-sided p=.90 table.
    assert len(SESSION_CLUSTER_T90_BY_DF) == 29
    # When / Then: regenerate every entry, not just a selected anchor.
    errors = [
        abs(student_t_quantile(df, 0.90) - float(expected))
        for df, expected in SESSION_CLUSTER_T90_BY_DF.items()
    ]
    assert max(errors) < 5e-8
    print(f"quant-v6 cross-validation: 29/29; max absolute error={max(errors):.12g}")


def test_generator_matches_normal_limit_and_cauchy_closed_form() -> None:
    # Given / When: effectively infinite df, plus the analytically soluble df=1.
    normal_limit = student_t_quantile(1_000_000, 0.975)
    cauchy = student_t_quantile(1, 0.975)
    # Then: independent analytic anchors validate the inverse-CDF implementation.
    assert round(normal_limit, 4) == 1.9600
    assert abs(normal_limit - NormalDist().inv_cdf(0.975)) < 3e-6
    assert cauchy == pytest.approx(math.tan(math.pi * (0.975 - 0.5)), abs=5e-12)
    print(f"normal limit df=1000000: {normal_limit:.10f}")


def test_strategy_v2_table_matches_exact_generator_to_nine_decimal_places() -> None:
    from app.domain.strategy_v2.clustered_returns import DAY_CLUSTER_T95_BY_DF

    assert set(DAY_CLUSTER_T95_BY_DF) == set(range(1, 121))
    for df, expected in DAY_CLUSTER_T95_BY_DF.items():
        assert abs(student_t_quantile(df, 0.975) - float(expected)) < 5e-10
