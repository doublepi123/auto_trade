"""P331: adjusted Sharpe ratio — raw, autocorrelation-corrected, moments-corrected."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series


@dataclass(frozen=True)
class AdjustedSharpeResult:
    raw_sharpe: float
    autocorr_adjusted: float
    moments_adjusted: float
    autocorrelations: list[float]
    skewness: float
    kurtosis: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_sharpe": self.raw_sharpe,
            "autocorr_adjusted": self.autocorr_adjusted,
            "moments_adjusted": self.moments_adjusted,
            "autocorrelations": list(self.autocorrelations),
            "skewness": self.skewness,
            "kurtosis": self.kurtosis,
        }


def adjusted_sharpe_report(
    returns: list[float],
    *,
    periods_per_year: int = 252,
    max_lag: int = 10,
) -> AdjustedSharpeResult:
    """Compute raw Sharpe and two adjusted (autocorrelation, moments) versions.

    - Autocorrelation adjustment (Lo-MacKinlay style):
      adjusted = raw / sqrt(1 + 2 * sum_{j=1}^{max_lag} rho_j)
    - Moments adjustment (Harvey-Siddique approximation):
      adjusted ≈ raw * sqrt(1 - skew*raw/6 + (kurt-3)*raw^2/24)

    Returns a tuple (raw, autocorr_adj, moments_adj).
    """
    r = validate_series(returns, name="returns", min_len=2)
    n = len(r)

    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int):
        raise ValueError("periods_per_year must be an int >= 1")
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be >= 1")
    if isinstance(max_lag, bool) or not isinstance(max_lag, int):
        raise ValueError("max_lag must be an int >= 1")
    if max_lag < 1:
        raise ValueError("max_lag must be >= 1")
    if max_lag > n - 1:
        max_lag = n - 1

    mu = mean(r)
    sigma = std(r, sample=True)
    ann_factor = math.sqrt(periods_per_year)

    # Raw Sharpe
    if sigma == 0:
        raw_sharpe = 0.0
    else:
        raw_sharpe = mu / sigma * ann_factor

    # Autocorrelations for lags 1..max_lag
    auto_corrs: list[float] = []
    for lag in range(1, max_lag + 1):
        if n > lag + 1:
            x = r[lag:]
            y = r[:-lag]
            mx = mean(x)
            my = mean(y)
            sx = math.sqrt(sum((v - mx) ** 2 for v in x))
            sy = math.sqrt(sum((v - my) ** 2 for v in y))
            if sx > 0 and sy > 0:
                rho = sum((x[i] - mx) * (y[i] - my) for i in range(len(x))) / (sx * sy)
                rho = max(-1.0, min(1.0, rho))
            else:
                rho = 0.0
        else:
            rho = 0.0
        auto_corrs.append(rho)

    # Autocorrelation-adjusted Sharpe
    if sigma == 0:
        autocorr_adjusted = 0.0
    else:
        rho_sum = sum(auto_corrs[:max_lag])
        variance_ratio = 1.0 + 2.0 * rho_sum
        if variance_ratio <= 0:
            autocorr_adjusted = 0.0
        else:
            autocorr_adjusted = raw_sharpe / math.sqrt(variance_ratio)

    # Skewness and kurtosis (sample)
    if sigma == 0:
        skew = 0.0
        kurt = 0.0
    else:
        z = [(v - mu) / sigma for v in r]
        m3 = mean([zi ** 3 for zi in z])
        m4 = mean([zi ** 4 for zi in z])
        # Sample adjustment factor
        skew = m3 * (n * n / ((n - 1) * (n - 2))) if n > 2 else 0.0
        # Excess kurtosis (sample adjusted)
        if n > 3:
            kurt_raw = m4 * (n * (n + 1) / ((n - 1) * (n - 2) * (n - 3))) - 3.0 * (n - 1) * (n - 1) / ((n - 2) * (n - 3))
        else:
            kurt_raw = 0.0
        kurt = kurt_raw + 3.0  # return regular kurtosis (not excess)

    # Moments-adjusted Sharpe (Harvey-Siddique approximation)
    if sigma == 0:
        moments_adjusted = 0.0
    else:
        # Convert back to non-annualized for the formula
        raw_non_annual = mu / sigma
        inner = 1.0 - skew * raw_non_annual / 6.0 + (kurt - 3.0) * raw_non_annual * raw_non_annual / 24.0
        if inner <= 0:
            moments_adjusted = 0.0
        else:
            moments_adjusted = raw_sharpe * math.sqrt(inner)

    return AdjustedSharpeResult(
        raw_sharpe=raw_sharpe,
        autocorr_adjusted=autocorr_adjusted,
        moments_adjusted=moments_adjusted,
        autocorrelations=auto_corrs,
        skewness=skew,
        kurtosis=kurt,
    )
