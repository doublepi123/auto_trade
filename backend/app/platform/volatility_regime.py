"""P359: volatility regime classification.

Detect volatility regimes (low/medium/high) from a return series using
rolling-window realized volatility, quantile-based classification, and
regime-switch detection.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = ["VolatilityRegimeResult", "volatility_regime_report"]


@dataclass(frozen=True)
class VolatilityRegimeResult:
    regime_labels: list[str]
    switch_points: list[int]
    regime_stats: dict[str, dict[str, float]]
    persistence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_labels": self.regime_labels,
            "switch_points": self.switch_points,
            "regime_stats": dict(self.regime_stats),
            "persistence": self.persistence,
        }


def volatility_regime_report(
    returns: list[float],
    *,
    window: int = 20,
    n_quantiles: int = 3,
) -> VolatilityRegimeResult:
    """Compute rolling realized-volatility regimes from a return series.

    Args:
        returns: Non-empty list of finite returns.  Length ≥ window.
        window: Rolling window size for realized-volatility computation.
        n_quantiles: Number of quantile bins (default 3 → low/medium/high).

    Returns:
        VolatilityRegimeResult with regime_labels (one per bar, starting from
        index ``window-1`` with NaN-padded prefix), switch_points (indices
        where label changes), regime_stats (per-regime mean_vol, avg_duration,
        count), and persistence (lag-1 autocorrelation of the volatility series).
    """
    validated = validate_series(returns, name="returns", min_len=window)
    if isinstance(n_quantiles, bool) or not isinstance(n_quantiles, int) or n_quantiles < 2:
        raise ValueError("n_quantiles must be an int >= 2")
    n = len(validated)

    # Rolling realized volatility (sample std over window).
    rolling_vol: list[float] = []
    for i in range(n):
        start = max(0, i - window + 1)
        segment = validated[start : i + 1]
        if len(segment) < 2:
            rolling_vol.append(0.0)
        else:
            mu = sum(segment) / len(segment)
            var = sum((x - mu) ** 2 for x in segment) / (len(segment) - 1)
            rolling_vol.append(math.sqrt(var))

    # Quantile thresholds from the full volatility series.
    vol_sorted = sorted(rolling_vol)
    thresholds: list[float] = []
    for q_idx in range(1, n_quantiles):
        rank = q_idx / n_quantiles
        pos = rank * (len(vol_sorted) - 1)
        lo = int(math.floor(pos))
        hi = int(math.ceil(pos))
        if lo == hi:
            thresholds.append(vol_sorted[lo])
        else:
            frac = pos - lo
            thresholds.append(vol_sorted[lo] * (1.0 - frac) + vol_sorted[hi] * frac)

    # Label each bar.
    label_names = ["low"]
    if n_quantiles == 3:
        label_names = ["low", "medium", "high"]
    elif n_quantiles == 2:
        label_names = ["low", "high"]
    else:
        label_names = ["low"] + [f"q{i}" for i in range(1, n_quantiles - 1)] + ["high"]

    regime_labels: list[str] = []
    for vol in rolling_vol:
        bucket = 0
        for th in thresholds:
            if vol >= th:
                bucket += 1
            else:
                break
        if bucket >= len(label_names):
            bucket = len(label_names) - 1
        regime_labels.append(label_names[bucket])

    # Switch points.
    switch_points: list[int] = []
    for i in range(1, n):
        if regime_labels[i] != regime_labels[i - 1]:
            switch_points.append(i)

    # Regime stats: mean_vol, avg_duration, count.
    regime_stats: dict[str, dict[str, float]] = {}
    regime_runs: dict[str, list[float]] = {}
    regime_vols: dict[str, list[float]] = {}

    for i in range(n):
        label = regime_labels[i]
        regime_vols.setdefault(label, []).append(rolling_vol[i])

    # Duration runs.
    if n > 0:
        current_label = regime_labels[0]
        run_len = 1
        for i in range(1, n):
            if regime_labels[i] == current_label:
                run_len += 1
            else:
                regime_runs.setdefault(current_label, []).append(float(run_len))
                current_label = regime_labels[i]
                run_len = 1
        regime_runs.setdefault(current_label, []).append(float(run_len))

    for label in sorted(set(regime_labels)):
        vols = regime_vols.get(label, [])
        runs = regime_runs.get(label, [])
        mean_vol = sum(vols) / len(vols) if vols else 0.0
        avg_duration = sum(runs) / len(runs) if runs else 0.0
        regime_stats[label] = {
            "mean_vol": mean_vol,
            "avg_duration": avg_duration,
            "count": len(vols),
        }

    # Persistence: lag-1 autocorrelation of rolling_vol.
    persistence = _lag1_autocorr(rolling_vol)

    return VolatilityRegimeResult(
        regime_labels=regime_labels,
        switch_points=switch_points,
        regime_stats=regime_stats,
        persistence=persistence,
    )


def _lag1_autocorr(series: Sequence[float]) -> float:
    n = len(series)
    if n < 2:
        return 0.0
    mu = sum(series) / n
    numer = sum((series[i] - mu) * (series[i - 1] - mu) for i in range(1, n))
    denom = sum((x - mu) ** 2 for x in series)
    if denom == 0.0:
        return 0.0
    return numer / denom
