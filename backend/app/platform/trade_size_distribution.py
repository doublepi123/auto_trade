"""P367: Trade-size distribution analysis.

Volume distribution statistics: mean/std/skew/kurtosis, Pareto tail fitting
(Hill estimator), Hurst exponent (R/S analysis), round-lot analysis,
autocorrelation, and Gini concentration. Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class TradeSizeDistributionResult:
    distribution_stats: dict[str, float]
    pareto_alpha: float | None
    hurst_exponent: float | None
    round_lot_ratio: float
    size_autocorr_lag1: float
    concentration_gini: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "distribution_stats": self.distribution_stats,
            "pareto_alpha": self.pareto_alpha,
            "hurst_exponent": self.hurst_exponent,
            "round_lot_ratio": self.round_lot_ratio,
            "size_autocorr_lag1": self.size_autocorr_lag1,
            "concentration_gini": self.concentration_gini,
        }


def _validate_volumes(volumes: list[float]) -> list[float]:
    """Validate volumes list: non-empty, all finite positive numbers."""
    if not isinstance(volumes, list) or not volumes:
        raise ValueError("volumes must be a non-empty list of finite numbers")
    validated: list[float] = []
    for v in volumes:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("volumes entries must be finite numbers")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("volumes entries must be finite numbers")
        validated.append(f)
    return validated


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Sample standard deviation."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _skewness(xs: list[float]) -> float:
    """Sample skewness (adjusted Fisher-Pearson)."""
    n = len(xs)
    if n < 3:
        return 0.0
    m = _mean(xs)
    s = _std(xs)
    if s == 0.0:
        return 0.0
    skew = sum((x - m) ** 3 for x in xs) / n
    return skew / (s ** 3) * math.sqrt(n * (n - 1)) / (n - 2)


def _kurtosis(xs: list[float]) -> float:
    """Sample excess kurtosis."""
    n = len(xs)
    if n < 4:
        return 0.0
    m = _mean(xs)
    s = _std(xs)
    if s == 0.0:
        return 0.0
    kurt = sum((x - m) ** 4 for x in xs) / n
    kurt = kurt / (s ** 4)
    # Excess kurtosis adjustment
    return ((n + 1) * (kurt - 3) + 6) * (n - 1) / ((n - 2) * (n - 3))


def _hill_estimator(xs: list[float], top_frac: float = 0.1) -> float | None:
    """Hill estimator for Pareto tail index using top fraction of values."""
    n = len(xs)
    k = max(2, int(n * top_frac))
    if k >= n:
        return None
    sorted_xs = sorted(xs, reverse=True)
    threshold = sorted_xs[k - 1]
    # Only consider values strictly above the threshold
    exceedances = [x - threshold for x in sorted_xs[:k] if x > threshold]
    if len(exceedances) < 2:
        return None
    # Hill estimator: alpha = 1 / mean(log(exceedances / threshold))
    # Simplified: alpha = k / sum(log(x_i / x_(k)))
    x_k = sorted_xs[k - 1]
    if x_k <= 0:
        return None
    log_sum = 0.0
    count = 0
    for i in range(k):
        if sorted_xs[i] > x_k:
            log_sum += math.log(sorted_xs[i] / x_k)
            count += 1
    if count < 2 or log_sum == 0.0:
        return None
    return count / log_sum


def _hurst_rs(series: list[float]) -> float | None:
    """R/S Hurst exponent estimate."""
    n = len(series)
    if n < 4:
        return None
    sizes: list[int] = []
    k = 2
    while k <= n // 2:
        sizes.append(k)
        k *= 2
    if not sizes:
        return None

    log_k: list[float] = []
    log_rs: list[float] = []
    for size in sizes:
        num_blocks = n // size
        rs_values: list[float] = []
        for b in range(num_blocks):
            block = series[b * size : (b + 1) * size]
            m = _mean(block)
            # Cumulative deviation
            cum_dev = 0.0
            max_dev = 0.0
            min_dev = 0.0
            for x in block:
                cum_dev += x - m
                if cum_dev > max_dev:
                    max_dev = cum_dev
                if cum_dev < min_dev:
                    min_dev = cum_dev
            r = max_dev - min_dev
            s = _std(block)
            if s > 0:
                rs_values.append(r / s)
        if not rs_values:
            continue
        mean_rs = sum(rs_values) / len(rs_values)
        log_k.append(math.log(size))
        log_rs.append(math.log(mean_rs))

    if len(log_k) < 2:
        return None

    m = len(log_k)
    sum_x = sum(log_k)
    sum_y = sum(log_rs)
    sum_xy = sum(x * y for x, y in zip(log_k, log_rs))
    sum_xx = sum(x * x for x in log_k)
    denom = m * sum_xx - sum_x * sum_x
    if denom == 0.0:
        return None
    slope = (m * sum_xy - sum_x * sum_y) / denom
    return max(0.0, min(1.0, slope))


def _autocorr_lag1(xs: list[float]) -> float:
    """Lag-1 autocorrelation."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    num = 0.0
    den = 0.0
    for i in range(n - 1):
        num += (xs[i] - m) * (xs[i + 1] - m)
    for x in xs:
        den += (x - m) ** 2
    if den == 0.0:
        return 0.0
    return num / den


def _gini(xs: list[float]) -> float:
    """Gini coefficient of concentration."""
    n = len(xs)
    if n < 2:
        return 0.0
    sorted_xs = sorted(xs)
    total = sum(sorted_xs)
    if total == 0.0:
        return 0.0
    cumsum = 0.0
    gini_sum = 0.0
    for i, x in enumerate(sorted_xs):
        cumsum += x
        gini_sum += (i + 1) * x
    # Gini = (2 * sum(i*x_i) / (n * sum(x_i))) - (n+1)/n
    return (2.0 * gini_sum) / (n * total) - (n + 1) / n


def trade_size_distribution_report(
    volumes: list[float], *, round_lot: float = 100.0
) -> TradeSizeDistributionResult:
    """Compute trade-size distribution statistics.

    Parameters
    ----------
    volumes:
        List of trade volumes.
    round_lot:
        Round lot size for round-lot ratio computation.

    Returns
    -------
    TradeSizeDistributionResult with distribution stats, Pareto alpha,
    Hurst exponent, round-lot ratio, autocorrelation, and Gini.
    """
    validated = _validate_volumes(volumes)
    n = len(validated)

    # Distribution statistics
    dist_stats: dict[str, float] = {
        "mean": _mean(validated),
        "std": _std(validated),
        "skew": _skewness(validated),
        "kurtosis": _kurtosis(validated),
        "min": min(validated),
        "max": max(validated),
        "count": float(n),
    }

    # Pareto tail fitting (Hill estimator on top 10%)
    pareto_alpha = _hill_estimator(validated)

    # Hurst exponent
    hurst = _hurst_rs(validated)

    # Round-lot analysis
    if round_lot > 0 and n > 0:
        round_count = sum(1 for v in validated if abs(v % round_lot) < 1e-10 or abs(v % round_lot - round_lot) < 1e-10)
        round_lot_ratio = round_count / n
    else:
        round_lot_ratio = 0.0

    # Autocorrelation lag-1
    size_autocorr = _autocorr_lag1(validated)

    # Gini concentration
    gini = _gini(validated)

    return TradeSizeDistributionResult(
        distribution_stats=dist_stats,
        pareto_alpha=pareto_alpha,
        hurst_exponent=hurst,
        round_lot_ratio=round_lot_ratio,
        size_autocorr_lag1=size_autocorr,
        concentration_gini=gini,
    )
