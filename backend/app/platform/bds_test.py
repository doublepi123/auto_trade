"""P385: BDS (Brock-Dechert-Scheinkman) test for independence.

Given a univariate time series, embed it in an m-dimensional phase space,
compute the correlation integral C_m(eps), and derive the BDS statistic
which is asymptotically N(0,1) under the null of i.i.d. data.

Pure Python — no numpy / scipy / statsmodels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = [
    "BdsTestResult",
    "bds_test_report",
]


@dataclass(frozen=True)
class BdsTestResult:
    """Frozen carrier for the BDS test report.

    Attributes
    ----------
    bds_statistic: The BDS test statistic (asymptotically N(0,1) under i.i.d.).
    correlation_integral_m: C_m(eps) for the given embedding dimension.
    correlation_integral_1: C_1(eps) for dimension 1.
    p_value: Two-sided asymptotic p-value from the normal approximation.
    is_independent: True when p_value > 0.05 (fail to reject independence).
    """

    bds_statistic: float
    correlation_integral_m: float
    correlation_integral_1: float
    p_value: float
    is_independent: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "bds_statistic": self.bds_statistic,
            "correlation_integral_m": self.correlation_integral_m,
            "correlation_integral_1": self.correlation_integral_1,
            "p_value": self.p_value,
            "is_independent": self.is_independent,
        }


def _correlation_integral(series: list[float], m: int, epsilon: float) -> float:
    """Compute the correlation integral C_m(eps).

    C_m(eps) = (2 / (T_m * (T_m - 1))) * sum_{
        i < j} I(||X_i - X_j|| < eps)

    where X_i = (series[i], series[i+1], ..., series[i+m-1]) is an m-history
    and the norm is the sup-norm (max absolute difference).
    """
    n = len(series)
    if n < m:
        raise ValueError(f"series length {n} must be >= embedding dimension {m}")
    t_m = n - m + 1  # number of m-histories
    if t_m < 2:
        return 0.0
    count = 0
    pairs = 0
    for i in range(t_m):
        for j in range(i + 1, t_m):
            pairs += 1
            # sup-norm
            max_diff = 0.0
            for k in range(m):
                diff = abs(series[i + k] - series[j + k])
                if diff > max_diff:
                    max_diff = diff
            if max_diff < epsilon:
                count += 1
    if pairs == 0:
        return 0.0
    return 2.0 * count / (t_m * (t_m - 1.0))


def _k_estimate(series: list[float], m: int, epsilon: float) -> float:
    """Estimate K = sigma_m / sigma for the BDS variance estimator.

    sigma_m^2 = 4 * [K^m + 2 * sum_{
        j=1}^{m-1} K^{m-j} * C_1^{2j} + (m-1)^2 * C_1^{2m} - m^2 * K * C_1^{2m-2}]
    where K = K(epsilon) is the probability that three points are within epsilon
    of each other. We approximate K via a triples count for dimension 1.
    """
    n = len(series)
    t_1 = n
    if epsilon <= 0 or t_1 < 3:
        return 0.0
    # Estimate K as the fraction of triples (i, j, k) with all pairwise
    # distances < epsilon, over the reference triple count.
    count_triples = 0
    total_triples = 0
    for i in range(t_1):
        for j in range(i + 1, t_1):
            for k in range(j + 1, t_1):
                total_triples += 1
                dij = abs(series[i] - series[j])
                dik = abs(series[i] - series[k])
                djk = abs(series[j] - series[k])
                if dij < epsilon and dik < epsilon and djk < epsilon:
                    count_triples += 1
    if total_triples == 0:
        return 0.0
    return count_triples / total_triples


def _normal_cdf(x: float) -> float:
    """Standard normal CDF via Horner-form polynomial approximation.

    Uses the Abramowitz & Stegun 26.2.17 approximation with maximum error
    |epsilon(x)| < 7.5e-8.
    """
    if x < -8.0:
        return 0.0
    if x > 8.0:
        return 1.0
    # Constants for erf approximation
    a1 = 0.254829592
    a2 = -0.284496736
    a3 = 1.421413741
    a4 = -1.453152027
    a5 = 1.061405429
    p = 0.3275911

    sign = 1.0 if x >= 0.0 else -1.0
    t = 1.0 / (1.0 + p * abs(x))
    y = 1.0 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * math.exp(-x * x / 2.0) / math.sqrt(2.0 * math.pi)
    # above is pdf * (a1*t + ...) which approximates the tail part
    # Actually let's use a simpler approach: erf approximation
    # Use the standard formula:
    # Phi(x) = 0.5 * (1 + erf(x / sqrt(2)))
    x_div = x / math.sqrt(2.0)
    t = 1.0 / (1.0 + p * abs(x_div))
    tau = t * (a1 + t * (a2 + t * (a3 + t * (a4 + t * a5))))
    erf_approx = sign * (1.0 - tau * math.exp(-x_div * x_div))
    return 0.5 * (1.0 + erf_approx)


def bds_test_report(
    series: list[float],
    *,
    embedding_dimension: int = 2,
    epsilon: float | None = None,
) -> BdsTestResult:
    """Run the BDS test for independence on a univariate series.

    Parameters
    ----------
    series: The time series to test.
    embedding_dimension: Embedding dimension m (default 2). Must be >= 2.
    epsilon: The distance threshold. Defaults to 0.7 * std(series).

    Returns
    -------
    BdsTestResult: Frozen dataclass with BDS statistic, correlation integrals,
                   p-value, and independence flag.

    Raises
    ------
    ValueError: If inputs are invalid.
    """
    validated = validate_series(series, name="series", min_len=10)
    n = len(validated)
    if isinstance(embedding_dimension, bool) or not isinstance(embedding_dimension, int):
        raise ValueError("embedding_dimension must be an int")
    if embedding_dimension < 2:
        raise ValueError("embedding_dimension must be >= 2")
    m = embedding_dimension
    if n < m + 1:
        raise ValueError(
            f"series length {n} must be > embedding dimension {m}"
        )

    # Compute standard deviation
    mu = sum(validated) / n
    var = sum((x - mu) ** 2 for x in validated) / (n - 1)
    std_dev = math.sqrt(var) if var > 0 else 1e-12

    eps = epsilon if epsilon is not None else 0.7 * std_dev
    if eps <= 0:
        raise ValueError("epsilon must be positive")

    # Compute correlation integrals
    c_1 = _correlation_integral(validated, 1, eps)
    c_m = _correlation_integral(validated, m, eps)

    t_m = n - m + 1

    # Estimate K for variance computation
    k_val = _k_estimate(validated, m, eps)
    if k_val <= 0:
        # Fallback: use a simple approximation
        k_val = c_1 ** 2

    # Compute sigma_m^2 (BDS variance)
    # Following Brock, Dechert, Scheinkman (1996), the variance estimator:
    # sigma^2 = 4 * [K^m + 2 * sum_{j=1}^{m-1} K^{m-j} * C^{2j} + (m-1)^2 * C^{2m} - m^2 * K * C^{2m-2}]
    # where C = c_1 and K = k_val.
    c = c_1
    k = k_val

    # Compute the sum term
    sum_term = 0.0
    for j in range(1, m):
        sum_term += (k ** (m - j)) * (c ** (2 * j))

    sigma_sq = 4.0 * (
        k ** m
        + 2.0 * sum_term
        + (m - 1) ** 2 * c ** (2 * m)
        - m ** 2 * k * c ** (2 * m - 2)
    )

    if sigma_sq <= 0:
        # Fallback: use asymptotic approximation
        sigma_sq = 1e-12

    sigma_m = math.sqrt(max(sigma_sq, 0.0))

    if sigma_m <= 0 or c_m <= 0 or c_1 <= 0:
        # Degenerate case: all points identical
        return BdsTestResult(
            bds_statistic=0.0,
            correlation_integral_m=c_m,
            correlation_integral_1=c_1,
            p_value=1.0,
            is_independent=True,
        )

    # BDS statistic
    # H0: c_m = c_1^m  (independence)
    # BDS = sqrt(T_m) * (c_m - c_1^m) / sigma_m
    numerator = c_m - c_1 ** m
    denominator = sigma_m / math.sqrt(max(t_m, 1))
    if denominator <= 0:
        bds = 0.0
    else:
        bds = numerator / denominator

    # Two-sided p-value
    abs_bds = abs(bds)
    p_value = 2.0 * (1.0 - _normal_cdf(abs_bds))
    p_value = max(0.0, min(1.0, p_value))

    is_independent = p_value > 0.05

    return BdsTestResult(
        bds_statistic=bds,
        correlation_integral_m=c_m,
        correlation_integral_1=c_1,
        p_value=p_value,
        is_independent=is_independent,
    )
