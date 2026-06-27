"""P319: Statistical feature extraction from a single numeric series.

Pure-Python feature extractor: computes 11 common statistical features from a
univariate series without numpy/scipy/pandas. Features include central moments,
range-based statistics, autocorrelation, trend slope, volatility clustering,
and maximum drawdown.

Public surface
--------------

* **feature_extraction_report(values)** → frozen
  :class:`FeatureExtractionResult` with a ``features`` dict.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean as _mean, std as _std, validate_series

__all__ = ["FeatureExtractionResult", "feature_extraction_report"]


@dataclass(frozen=True)
class FeatureExtractionResult:
    features: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {"features": dict(self.features)}


def feature_extraction_report(values: list[float]) -> FeatureExtractionResult:
    series = validate_series(values, name="values", min_len=2)

    n = len(series)
    mu = _mean(series)
    sigma = _std(series, sample=False)

    # Skewness and kurtosis (excess kurtosis)
    if sigma == 0.0:
        skew = 0.0
        kurt = 0.0
    else:
        z_scores = [(v - mu) / sigma for v in series]
        m3 = sum(z ** 3 for z in z_scores) / n
        m4 = sum(z ** 4 for z in z_scores) / n
        skew = m3
        kurt = m4 - 3.0  # excess kurtosis

    # Range-based
    lo = min(series)
    hi = max(series)

    # Autocorrelation lag-1
    if sigma == 0.0 or n < 2:
        autocorr_lag1 = 1.0 if sigma == 0.0 else 0.0
    else:
        cov = sum((series[i] - mu) * (series[i + 1] - mu) for i in range(n - 1)) / (n - 1)
        autocorr_lag1 = cov / (sigma * sigma) if sigma != 0.0 else 0.0
        # Clamp to [-1, 1]
        autocorr_lag1 = max(-1.0, min(1.0, autocorr_lag1))

    # Trend slope (OLS through indices)
    if n < 2:
        trend_slope = 0.0
    else:
        x_mean = (n - 1) / 2.0
        y_mean = mu
        num = sum((i - x_mean) * (series[i] - y_mean) for i in range(n))
        den = sum((i - x_mean) ** 2 for i in range(n))
        trend_slope = num / den if den != 0.0 else 0.0

    # Volatility clustering (autocorrelation of squared returns)
    if n < 3 or sigma == 0.0:
        vol_clustering = 0.0
    else:
        returns = [(series[i] - series[i - 1]) for i in range(1, n)]
        sq_returns = [r * r for r in returns]
        r_mu = _mean(sq_returns)
        r_sigma = _std(sq_returns, sample=False)
        if r_sigma == 0.0:
            vol_clustering = 0.0
        else:
            cov_r = sum((sq_returns[i] - r_mu) * (sq_returns[i + 1] - r_mu) for i in range(len(sq_returns) - 1)) / (len(sq_returns) - 1)
            vol_clustering = max(-1.0, min(1.0, cov_r / (r_sigma * r_sigma)))

    # Maximum drawdown
    peak = series[0]
    mdd = 0.0
    for v in series:
        if v > peak:
            peak = v
        if peak > 0.0:
            dd = (peak - v) / peak
            if dd > mdd:
                mdd = dd

    return FeatureExtractionResult(
        features={
            "mean": mu,
            "std": sigma,
            "skew": skew,
            "kurt": kurt,
            "min": lo,
            "max": hi,
            "range": hi - lo,
            "autocorr_lag1": autocorr_lag1,
            "trend_slope": trend_slope,
            "volatility_clustering": vol_clustering,
            "max_drawdown": mdd,
        }
    )
