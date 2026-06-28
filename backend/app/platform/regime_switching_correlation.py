"""P366: Regime-switching correlation analysis.

Rolling-window average pairwise correlation with low/high regime classification
via P50 quantile split. Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


_MAX_ASSETS = 50


@dataclass(frozen=True)
class RegimeSwitchingCorrelationResult:
    regime_path: list[str]
    avg_correlation_series: list[float]
    high_regime_stats: dict[str, float]
    low_regime_stats: dict[str, float]
    diversification_premium: float
    n_assets: int
    window: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "regime_path": self.regime_path,
            "avg_correlation_series": self.avg_correlation_series,
            "high_regime_stats": self.high_regime_stats,
            "low_regime_stats": self.low_regime_stats,
            "diversification_premium": self.diversification_premium,
            "n_assets": self.n_assets,
            "window": self.window,
        }


def _validate_panel(
    returns_panel: dict[str, list[float]], window: int
) -> dict[str, list[float]]:
    """Validate returns panel: non-empty, ≤50 assets, equal-length, finite values."""
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) < 2:
        raise ValueError("returns_panel must contain at least 2 assets")
    if len(returns_panel) > _MAX_ASSETS:
        raise ValueError(f"returns_panel must contain at most {_MAX_ASSETS} assets")

    validated: dict[str, list[float]] = {}
    length: int | None = None
    for name, series in returns_panel.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
        if length is None:
            length = len(series)
        elif len(series) != length:
            raise ValueError("returns_panel series must have equal length")
        vec: list[float] = []
        for v in series:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(f"returns_panel['{name}'] entries must be finite numbers")
            f = float(v)
            if not math.isfinite(f):
                raise ValueError(f"returns_panel['{name}'] entries must be finite numbers")
            vec.append(f)
        validated[str(name)] = vec
    return validated


def _pearson_corr(x: list[float], y: list[float]) -> float:
    """Pearson correlation coefficient between two equal-length series."""
    n = len(x)
    if n < 2:
        return 0.0
    mx = sum(x) / n
    my = sum(y) / n
    sx = 0.0
    sy = 0.0
    sxy = 0.0
    for xi, yi in zip(x, y):
        dx = xi - mx
        dy = yi - my
        sx += dx * dx
        sy += dy * dy
        sxy += dx * dy
    denom = math.sqrt(sx * sy)
    if denom == 0.0:
        return 0.0
    return sxy / denom


def _avg_pairwise_corr(
    panel: dict[str, list[float]], start: int, end: int
) -> float:
    """Average pairwise Pearson correlation in window [start, end)."""
    assets = list(panel.keys())
    if len(assets) < 2:
        return 0.0
    corrs: list[float] = []
    for i in range(len(assets)):
        for j in range(i + 1, len(assets)):
            xi = panel[assets[i]][start:end]
            xj = panel[assets[j]][start:end]
            corrs.append(_pearson_corr(xi, xj))
    if not corrs:
        return 0.0
    return sum(corrs) / len(corrs)


def _median(values: list[float]) -> float:
    """Compute the median of a list of floats."""
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2.0


def _regime_stats(
    corr_series: list[float], regime_path: list[str], target_regime: str
) -> dict[str, float]:
    """Compute stats for a specific regime."""
    indices = [i for i, r in enumerate(regime_path) if r == target_regime]
    if not indices:
        return {"mean_corr": 0.0, "frequency": 0.0, "avg_duration": 0.0}

    regime_corrs = [corr_series[i] for i in indices]
    mean_corr = sum(regime_corrs) / len(regime_corrs)
    frequency = len(indices) / len(regime_path)

    # Average duration: count consecutive runs
    durations: list[int] = []
    current_run = 0
    for r in regime_path:
        if r == target_regime:
            current_run += 1
        else:
            if current_run > 0:
                durations.append(current_run)
            current_run = 0
    if current_run > 0:
        durations.append(current_run)
    avg_duration = sum(durations) / len(durations) if durations else 0.0

    return {
        "mean_corr": mean_corr,
        "frequency": frequency,
        "avg_duration": avg_duration,
    }


def regime_switching_correlation_report(
    returns_panel: dict[str, list[float]], *, window: int = 20
) -> RegimeSwitchingCorrelationResult:
    """Rolling-window average pairwise correlation with low/high regime classification.

    Parameters
    ----------
    returns_panel:
        Dict mapping asset name to list of period returns.
    window:
        Rolling window size (default 20).

    Returns
    -------
    RegimeSwitchingCorrelationResult with regime_path, avg_correlation_series,
    regime stats, and diversification_premium.
    """
    validated = _validate_panel(returns_panel, window)
    n_obs = len(next(iter(validated.values())))
    if n_obs < window:
        raise ValueError(f"series length must be >= window ({window})")

    # Compute rolling average pairwise correlation
    avg_corr_series: list[float] = []
    for start in range(n_obs - window + 1):
        end = start + window
        avg_corr_series.append(_avg_pairwise_corr(validated, start, end))

    if not avg_corr_series:
        return RegimeSwitchingCorrelationResult(
            regime_path=[],
            avg_correlation_series=[],
            high_regime_stats={"mean_corr": 0.0, "frequency": 0.0, "avg_duration": 0.0},
            low_regime_stats={"mean_corr": 0.0, "frequency": 0.0, "avg_duration": 0.0},
            diversification_premium=0.0,
            n_assets=len(validated),
            window=window,
        )

    # P50 quantile split into low/high regimes
    threshold = _median(avg_corr_series)
    regime_path = ["high" if c >= threshold else "low" for c in avg_corr_series]

    high_stats = _regime_stats(avg_corr_series, regime_path, "high")
    low_stats = _regime_stats(avg_corr_series, regime_path, "low")

    diversification_premium = low_stats["mean_corr"] - high_stats["mean_corr"]

    return RegimeSwitchingCorrelationResult(
        regime_path=regime_path,
        avg_correlation_series=avg_corr_series,
        high_regime_stats=high_stats,
        low_regime_stats=low_stats,
        diversification_premium=diversification_premium,
        n_assets=len(validated),
        window=window,
    )
