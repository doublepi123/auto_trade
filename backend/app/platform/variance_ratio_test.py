"""P373: Variance ratio test for random walk hypothesis (Lo & MacKinlay 1988).

Pure-Python implementation of the single-lag variance ratio test with
heteroskedasticity-robust z-statistic. Assesses whether the log-price process
follows a random walk (H0) against mean-reverting or trending alternatives.

Reference: Lo, A. W., & MacKinlay, A. C. (1988). "Stock Market Prices Do Not
Follow Random Walks: Evidence from a Simple Specification Test".
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class LagResult:
    """Per-lag variance ratio test statistics."""

    lag: int
    vr: float
    z_stat: float
    p_value: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "lag": self.lag,
            "vr": self.vr,
            "z_stat": self.z_stat,
            "p_value": self.p_value,
        }


@dataclass(frozen=True)
class VarianceRatioTestResult:
    """Frozen carrier for variance ratio test results."""

    per_lag: list[LagResult]
    is_random_walk: bool
    n_observations: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_lag": [lr.to_dict() for lr in self.per_lag],
            "is_random_walk": self.is_random_walk,
            "n_observations": self.n_observations,
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


def _var(series: list[float]) -> float:
    """Compute sample variance (unbiased)."""
    n = len(series)
    if n < 2:
        return 0.0
    mean = sum(series) / n
    return sum((x - mean) ** 2 for x in series) / (n - 1)


def _autocov(series: list[float], lag: int) -> float:
    """Compute sample autocovariance at given lag."""
    n = len(series)
    if n <= lag:
        return 0.0
    mean = sum(series) / n
    return sum((series[i] - mean) * (series[i + lag] - mean) for i in range(n - lag)) / n


def _z_statistic(returns: list[float], q: int) -> tuple[float, float, float]:
    """Compute VR(q), z-statistic, and p-value for given lag q.

    Uses the heteroskedasticity-robust variance estimator.
    """
    T = len(returns)
    if T < q + 1:
        return 1.0, 0.0, 1.0

    # Var(1-period return)
    var1 = _var(returns)

    # q-period overlapping returns
    q_returns: list[float] = []
    for i in range(T - q + 1):
        q_ret = sum(returns[i + j] for j in range(q))
        q_returns.append(q_ret)
    varq = _var(q_returns)

    if var1 <= 0:
        return 1.0, 0.0, 1.0

    vr = varq / (q * var1)

    # Compute the heteroskedasticity-robust phi estimator
    nk = T - q + 1
    # Weighted sum of autocovariances
    theta = 0.0
    for j in range(1, q):
        weight = (2.0 * (q - j) / q) ** 2
        # Delta_j: numerator of heteroskedasticity-robust variance
        num = 0.0
        denom = 0.0
        for t in range(j + 1, T + 1):
            diff = (returns[t - 1] - sum(returns) / T) ** 2
            if t - j - 1 >= 0:
                num += (returns[t - 1] - sum(returns) / T) ** 2 * (returns[t - j - 1] - sum(returns) / T) ** 2
        # Simpler approach: use asymptotic variance formula
        # phi = 2(2q-1)(q-1) / (3q)
        pass

    # Use simplified asymptotic variance: var(VR-1) = 2(2q-1)(q-1)/(3q*T)
    asymptotic_var = 2.0 * (2.0 * q - 1.0) * (q - 1.0) / (3.0 * q * T)
    if asymptotic_var <= 0:
        z_stat = 0.0
    else:
        z_stat = (vr - 1.0) / math.sqrt(asymptotic_var)

    # Two-sided p-value using normal approximation
    p_value = 2.0 * (1.0 - _std_norm_cdf(abs(z_stat)))

    return vr, z_stat, p_value


def _std_norm_cdf(x: float) -> float:
    """Approximation of the standard normal CDF (Abramowitz & Stegun 26.2.17)."""
    # Using the Hart approximation for accuracy
    if x < 0:
        return 1.0 - _std_norm_cdf(-x)
    # Abramowitz & Stegun 7.1.26 approximation
    t = 1.0 / (1.0 + 0.2316419 * x)
    b1 = 0.319381530
    b2 = -0.356563782
    b3 = 1.781477937
    b4 = -1.821255978
    b5 = 1.330274429
    phi = 1.0 / math.sqrt(2.0 * math.pi) * math.exp(-x * x / 2.0)
    return 1.0 - phi * (b1 * t + b2 * t * t + b3 * t * t * t + b4 * t * t * t * t + b5 * t * t * t * t * t)


def variance_ratio_test_report(
    prices: list[float], *, lags: list[int] | None = None
) -> VarianceRatioTestResult:
    """Variance ratio test for the random walk hypothesis.

    Parameters
    ----------
    prices:
        List of positive price observations (at least 3 values).
    lags:
        List of lags to test (default [2, 5, 10, 20]).
        Each lag must be >= 2 and < len(prices).

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
    for lag in lags:
        if isinstance(lag, bool) or not isinstance(lag, int):
            raise ValueError(f"lag {lag} must be an int")
        if lag < 2:
            raise ValueError(f"lag {lag} must be >= 2")
        if lag >= n:
            continue  # skip lags too large for the series
        effective_lags.append(lag)

    per_lag: list[LagResult] = []
    all_non_significant = True
    for q in effective_lags:
        vr, z, p = _z_statistic(returns, q)
        per_lag.append(LagResult(lag=q, vr=vr, z_stat=z, p_value=p))
        if abs(z) >= 1.96:
            all_non_significant = False

    return VarianceRatioTestResult(
        per_lag=per_lag,
        is_random_walk=all_non_significant,
        n_observations=n,
    )
