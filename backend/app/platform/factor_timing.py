"""P362: Factor timing — valuation, crowding, and momentum composite signal.

Pure-Python factor timing estimator: for each factor computes
valuation_z (recent IC vs full-sample), crowding (lag-1 factor return autocorr),
momentum (recent cumulative return), and blends them into a timing_score
that drives an overweight / underweight / neutral tilt.

Public surface
--------------

* **factor_timing_report(factor_ic, factor_returns, lookback)** → frozen
  :class:`FactorTimingResult` with ``factor_signals`` (list of per-factor dicts)
  and ``ranking`` (factor names ordered by descending timing_score).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series

__all__ = ["FactorTimingResult", "factor_timing_report"]


def _autocorr_lag1(series: list[float]) -> float:
    """Compute lag-1 autocorrelation of a numeric series."""
    n = len(series)
    if n < 2:
        return 0.0
    m = mean(series)
    num = sum((series[t] - m) * (series[t - 1] - m) for t in range(1, n))
    den = sum((x - m) ** 2 for x in series)
    if den == 0.0:
        return 0.0
    return num / den


def _cum_recent(series: list[float], lookback: int) -> float:
    """Sum of the most recent `lookback` values."""
    return sum(series[-lookback:])


def _zscore_scalar(values: list[float], *, lookback: int, full_mean: float, full_std: float) -> float:
    """Compute trailing z-score using global mean/std."""
    if full_std == 0.0:
        return 0.0
    recent_mean = mean(values[-lookback:])
    return (recent_mean - full_mean) / full_std


def _momentum_zscore(
    factor_name: str,
    momentum: dict[str, float],
    all_momentum: list[float],
) -> float:
    """Compute cross-sectional momentum z-score."""
    m_std = std(all_momentum)
    if m_std == 0.0:
        return 0.0
    m_mean = mean(all_momentum)
    return (momentum[factor_name] - m_mean) / m_std


@dataclass(frozen=True)
class FactorTimingResult:
    factor_signals: list[dict[str, Any]]
    ranking: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_signals": [dict(s) for s in self.factor_signals],
            "ranking": list(self.ranking),
        }


def factor_timing_report(
    factor_ic: dict[str, list[float]],
    factor_returns: dict[str, list[float]],
    *,
    lookback: int = 12,
) -> FactorTimingResult:
    if not isinstance(factor_ic, dict) or not factor_ic:
        raise ValueError("factor_ic must be a non-empty dict of IC series")
    if len(factor_ic) > 50:
        raise ValueError("factor_ic must contain at most 50 factors")
    if not isinstance(factor_returns, dict) or not factor_returns:
        raise ValueError("factor_returns must be a non-empty dict of return series")

    validated_ic: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in factor_ic.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"factor_ic['{name}'] must be a list of finite numbers")
        vec = validate_series(series, name=f"factor_ic['{name}']", min_len=2)
        if length is None:
            length = len(vec)
        elif len(vec) != length:
            raise ValueError("all factor IC series must have equal length")
        validated_ic[str(name)] = vec

    validated_ret: dict[str, list[float]] = {}
    for name, series in factor_returns.items():
        if isinstance(series, (str, dict)) or not isinstance(series, list):
            raise ValueError(f"factor_returns['{name}'] must be a list of finite numbers")
        vec = validate_series(series, name=f"factor_returns['{name}']", min_len=2)
        if len(vec) != length:
            raise ValueError("all factor return series must have equal length")
        validated_ret[str(name)] = vec

    # Ensure both dicts have the same factor names
    if validated_ic.keys() != validated_ret.keys():
        raise ValueError("factor_ic and factor_returns must have the same factor names")

    if isinstance(lookback, bool) or not isinstance(lookback, int):
        raise ValueError("lookback must be an int >= 2")
    if lookback < 2:
        raise ValueError("lookback must be an int >= 2")

    lb = min(lookback, length)  # type: ignore[arg-type]

    # Pre-compute per-factor IC full-sample stats
    ic_means: dict[str, float] = {}
    ic_stds: dict[str, float] = {}
    for name, series in validated_ic.items():
        ic_means[name] = mean(series)
        ic_stds[name] = std(series)

    # Per-factor momentum (cumulative recent returns)
    momentum: dict[str, float] = {}
    for name, series in validated_ret.items():
        momentum[name] = _cum_recent(series, lb)

    # Momentum z-score (cross-sectional)
    factor_names = list(validated_ret.keys())
    all_momentum_vals = [momentum[n] for n in factor_names]
    momentum_zscores: dict[str, float] = {}
    m_std = std(all_momentum_vals)
    m_mean = mean(all_momentum_vals)
    for name in factor_names:
        if m_std == 0.0:
            momentum_zscores[name] = 0.0
        else:
            momentum_zscores[name] = (momentum[name] - m_mean) / m_std

    # Build per-factor signals
    factor_signals: list[dict[str, Any]] = []
    for name in factor_names:
        ic_series = validated_ic[name]
        ret_series = validated_ret[name]

        valuation_z = _zscore_scalar(
            ic_series, lookback=lb, full_mean=ic_means[name], full_std=ic_stds[name]
        )
        crowding = _autocorr_lag1(ret_series)
        moment = momentum[name]
        mom_z = momentum_zscores[name]

        timing_score = 0.4 * valuation_z + 0.3 * (1.0 - crowding) + 0.3 * mom_z

        if timing_score > 0.5:
            tilt = "overweight"
        elif timing_score < -0.5:
            tilt = "underweight"
        else:
            tilt = "neutral"

        factor_signals.append({
            "factor": name,
            "valuation_z": round(valuation_z, 6),
            "crowding": round(crowding, 6),
            "momentum": round(moment, 6),
            "timing_score": round(timing_score, 6),
            "tilt": tilt,
        })

    # Ranking by descending timing_score
    ranking = sorted(factor_names, key=lambda n: next(
        s["timing_score"] for s in factor_signals if s["factor"] == n
    ), reverse=True)

    return FactorTimingResult(
        factor_signals=factor_signals,
        ranking=ranking,
    )
