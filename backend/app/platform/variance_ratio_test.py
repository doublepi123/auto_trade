"""P373: Variance ratio test for random walk hypothesis (Lo & MacKinlay 1988).

Pure-Python implementation of the single-lag variance ratio test with both
homoskedastic and heteroskedasticity-robust z-statistics. Assesses whether the
log-price process follows a random walk (H0) against mean-reverting or trending
alternatives.

Reference: Lo, A. W., & MacKinlay, A. C. (1988). "Stock Market Prices Do Not
Follow Random Walks: Evidence from a Simple Specification Test".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LagResult:
    """Per-lag variance ratio test statistics."""

    lag: int
    vr: float
    z_stat: float
    p_value: float
    z_robust: float
    p_one_sided_lower: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lag": self.lag,
            "vr": self.vr,
            "z_stat": self.z_stat,
            "p_value": self.p_value,
            "z_robust": self.z_robust,
            "p_one_sided_lower": self.p_one_sided_lower,
        }


@dataclass(frozen=True)
class VarianceRatioTestResult:
    """Frozen carrier for variance ratio test results."""

    per_lag: list[LagResult]
    is_random_walk: bool
    n_observations: int
    valid: bool
    invalid_reason: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_lag": [lr.to_dict() for lr in self.per_lag],
            "is_random_walk": self.is_random_walk,
            "n_observations": self.n_observations,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
        }


def _validate_prices(prices: list[float]) -> list[float]:
    """Validate the price series."""
    if not isinstance(prices, list) or not prices:
        raise ValueError("prices must be a non-empty list of finite numbers")
    if len(prices) < 3:
        raise ValueError("prices must contain at least 3 values")
    validated: list[float] = []
    for v in prices:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("prices entries must be finite numbers")
        f = float(v)
        if not math.isfinite(f) or f <= 0:
            raise ValueError("prices entries must be finite positive numbers")
        validated.append(f)
    return validated


def _compute_returns(prices: list[float]) -> list[float]:
    """Compute log returns from prices."""
    n = len(prices)
    returns: list[float] = []
    for i in range(1, n):
        returns.append(math.log(prices[i] / prices[i - 1]))
    return returns


def _autocov(series: list[float], lag: int) -> float:
    """Compute sample autocovariance at given lag."""
    n = len(series)
    if n <= lag:
        return 0.0
    mean = sum(series) / n
    return sum((series[i] - mean) * (series[i + lag] - mean) for i in range(n - lag)) / n


def _z_statistic(
    returns: list[float], q: int
) -> tuple[float, float, float, float, float]:
    """Compute VR(q), homoskedastic and robust statistics for lag q.

    Uses the homoskedastic (Lo-MacKinlay 1988 M1 statistic) variance estimator.
    """
    T = len(returns)
    if T < q + 1:
        return 1.0, 0.0, 1.0, 0.0, 0.5

    mean = sum(returns) / T
    demeaned = [value - mean for value in returns]
    sum_squared = sum(value * value for value in demeaned)

    if sum_squared <= 0.0:
        return 1.0, 0.0, 1.0, 0.0, 0.5

    # Lo-MacKinlay eq. 11-12: debias overlapping sums with m, and use T/(T-1)
    # for the one-period variance.
    variance_one_period = sum_squared / (T - 1)
    m = q * (T - q + 1) * (1.0 - q / T)
    q_sum_squared = sum(
        (sum(returns[index : index + q]) - q * mean) ** 2
        for index in range(T - q + 1)
    )
    variance_q_period = q_sum_squared / m
    vr = variance_q_period / variance_one_period

    # Use asymptotic variance: var(VR-1) = 2(2q-1)(q-1)/(3q*T)
    asymptotic_var = 2.0 * (2.0 * q - 1.0) * (q - 1.0) / (3.0 * q * T)
    if asymptotic_var <= 0:
        z_stat = 0.0
    else:
        z_stat = (vr - 1.0) / math.sqrt(asymptotic_var)

    # Two-sided p-value using normal approximation
    p_value = 2.0 * (1.0 - _std_norm_cdf(abs(z_stat)))

    # theta(q) = sum_{k=1}^{q-1} 4(1-k/q)^2 delta_k, with delta_k based on
    # products of squared demeaned one-period returns.
    theta = 0.0
    denominator = sum_squared * sum_squared
    for k in range(1, q):
        cross_product = sum(
            demeaned[index] ** 2 * demeaned[index - k] ** 2
            for index in range(k, T)
        )
        delta = T * cross_product / denominator
        theta += 4.0 * (1.0 - k / q) ** 2 * delta
    z_robust = (
        math.sqrt(T) * (vr - 1.0) / math.sqrt(theta) if theta > 0.0 else 0.0
    )
    p_one_sided_lower = _std_norm_cdf(z_robust)

    return vr, z_stat, p_value, z_robust, p_one_sided_lower


def _std_norm_cdf(x: float) -> float:
    """Compute the standard normal cumulative distribution function."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def variance_ratio_test_report(
    prices: list[float],
    *,
    lags: list[int] | None = None,
    min_observations: int = 200,
    min_n_over_q: int = 10,
) -> VarianceRatioTestResult:
    """Variance ratio test for the random walk hypothesis.

    Parameters
    ----------
    prices:
        List of positive price observations (at least 3 values).
    lags:
        List of lags to test (default [2, 5, 10, 20]).
        Each lag must be >= 2 and < len(prices).
    min_observations:
        Minimum number of prices required for a statistically valid result.
    min_n_over_q:
        Minimum observations-to-lag ratio required for each reported lag.

    Returns
    -------
    VarianceRatioTestResult with per_lag statistics and is_random_walk flag.
    """
    validated = _validate_prices(prices)
    n = len(validated)
    returns = _compute_returns(validated)

    if lags is None:
        lags = [2, 5, 10, 20]

    effective_lags: list[int] = []
    rejected_reasons: list[str] = []
    for lag in lags:
        if isinstance(lag, bool) or not isinstance(lag, int):
            raise ValueError(f"lag {lag} must be an int")
        if lag < 2:
            raise ValueError(f"lag {lag} must be >= 2")
        if lag >= n:
            continue  # skip lags too large for the series
        # Trade-price VR(2) is mechanically depressed by Roll bid-ask bounce,
        # so it cannot identify economic mean reversion.
        if lag == 2:
            rejected_reasons.append("lag 2 rejected due to bid-ask bounce")
            continue
        if n / lag < min_n_over_q:
            rejected_reasons.append(
                f"lag {lag} rejected because n/q={n / lag:.2f} is below "
                f"{min_n_over_q}"
            )
            continue
        effective_lags.append(lag)

    if not effective_lags:
        reason = "; ".join(rejected_reasons)
        if not reason:
            raise ValueError(
                f"all requested lags {lags} are >= series length {n}; "
                f"no effective lags remain"
            )
        return VarianceRatioTestResult(
            per_lag=[],
            is_random_walk=False,
            n_observations=n,
            valid=False,
            invalid_reason=reason,
        )

    per_lag: list[LagResult] = []
    all_non_significant = True
    for q in effective_lags:
        vr, z, p, z_robust, p_one_sided_lower = _z_statistic(returns, q)
        per_lag.append(
            LagResult(
                lag=q,
                vr=vr,
                z_stat=z,
                p_value=p,
                z_robust=z_robust,
                p_one_sided_lower=p_one_sided_lower,
            )
        )
        if abs(z) >= 1.96:
            all_non_significant = False

    observation_reason = None
    if n < min_observations:
        observation_reason = (
            f"{n} observations are below the minimum {min_observations}"
        )
    invalid_reason = "; ".join(
        reason for reason in [observation_reason, *rejected_reasons] if reason
    ) or None
    valid = observation_reason is None

    return VarianceRatioTestResult(
        per_lag=per_lag,
        is_random_walk=all_non_significant if valid else False,
        n_observations=n,
        valid=valid,
        invalid_reason=invalid_reason,
    )
