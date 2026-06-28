"""P353: Relative Rotation Graph (RRG) analysis.

Classifies assets into 4 quadrants (leading/improving/lagging/weakening)
based on their relative strength vs. a benchmark, following the RRG
methodology: normalize RS to 100, compute z-score (RS-Ratio) and its
rate-of-change (RS-Momentum), then quadrant classification.

Public surface
--------------
* **relative_rotation_report(assets, benchmark, tail)** — frozen
  :class:`RelativeRotationResult` with per-asset RS ratio, momentum,
  quadrant classification, and latest quadrant snapshot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "RelativeRotationResult",
    "relative_rotation_report",
]

_MAX_ASSETS = 50
_MIN_SERIES = 3
"""Minimum series length for rolling mean/std."""


# ---------------------------------------------------------------------------
# validation helpers
# ---------------------------------------------------------------------------


def _validate_benchmark(benchmark: list[float], min_length: int) -> list[float]:
    """Validate the benchmark series."""
    if not isinstance(benchmark, list):
        raise ValueError("benchmark must be a list of finite numbers")
    if len(benchmark) < min_length:
        raise ValueError(f"benchmark must contain at least {min_length} values")
    result: list[float] = []
    for value in benchmark:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("benchmark entries must be finite numbers")
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("benchmark entries must be finite numbers")
        result.append(number)
    return result


def _validate_assets(assets: dict[str, list[float]], benchmark_len: int) -> dict[str, list[float]]:
    """Validate the assets panel."""
    if not isinstance(assets, dict) or not assets:
        raise ValueError("assets must be a non-empty dict")
    if len(assets) > _MAX_ASSETS:
        raise ValueError(f"assets must contain at most {_MAX_ASSETS} entries")
    validated: dict[str, list[float]] = {}
    for name, series in assets.items():
        if not isinstance(series, list):
            raise ValueError(f"assets['{name}'] must be a list of finite numbers")
        if len(series) != benchmark_len:
            raise ValueError(f"assets['{name}'] length must match benchmark length ({benchmark_len})")
        vec: list[float] = []
        for value in series:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"assets['{name}'] entries must be finite numbers")
            number = float(value)
            if not math.isfinite(number):
                raise ValueError(f"assets['{name}'] entries must be finite numbers")
            vec.append(number)
        validated[str(name)] = vec
    return validated


# ---------------------------------------------------------------------------
# rolling mean / std
# ---------------------------------------------------------------------------


def _rolling_mean(series: list[float], window: int) -> list[float]:
    """Rolling mean (SMA). First ``window - 1`` entries are NaN-padded with the
    expanding-window mean to avoid data loss.
    """
    n = len(series)
    result: list[float] = [0.0] * n
    if n == 0:
        return result
    # Expanding window for first window-1 entries
    cumsum = 0.0
    for i in range(min(window, n)):
        cumsum += series[i]
        result[i] = cumsum / (i + 1)
    # Rolling window thereafter
    running = cumsum
    for i in range(window, n):
        running += series[i] - series[i - window]
        result[i] = running / window
    return result


def _rolling_std(series: list[float], mean: list[float], window: int) -> list[float]:
    """Rolling population std."""
    n = len(series)
    result: list[float] = [0.0] * n
    if n == 0:
        return result
    for i in range(min(window, n)):
        mu = mean[i]
        var_sum = 0.0
        for j in range(i + 1):
            diff = series[j] - mu
            var_sum += diff * diff
        result[i] = math.sqrt(var_sum / (i + 1)) if i > 0 else 0.0
    for i in range(window, n):
        mu = mean[i]
        var_sum = 0.0
        for j in range(i - window + 1, i + 1):
            diff = series[j] - mu
            var_sum += diff * diff
        result[i] = math.sqrt(var_sum / window)
    return result


# ---------------------------------------------------------------------------
# RRG computation
# ---------------------------------------------------------------------------


def _compute_rs_ratio(
    asset_prices: list[float], benchmark_prices: list[float], tail: int
) -> tuple[list[float], float, float]:
    """Compute RS-Ratio (z-score of RS) and RS-Momentum (diff of RS-Ratio).

    RS = (asset_price / benchmark_price) * 100.
    RS-Ratio = (RS - rolling_mean(RS, tail)) / rolling_std(RS, tail).
    RS-Momentum = diff(RS-Ratio).

    Returns (rs_ratio_series, latest_rs_ratio, latest_rs_momentum).
    """
    n = len(asset_prices)
    eps = 1e-12
    rs = [0.0] * n
    for i in range(n):
        denom = benchmark_prices[i]
        rs[i] = (asset_prices[i] / denom) * 100.0 if denom != 0.0 else 100.0

    rs_mean = _rolling_mean(rs, tail)
    rs_std = _rolling_std(rs, rs_mean, tail)

    rs_ratio: list[float] = [0.0] * n
    for i in range(n):
        denom = rs_std[i] if rs_std[i] > eps else eps
        rs_ratio[i] = (rs[i] - rs_mean[i]) / denom

    # RS-Momentum: rate of change of RS-Ratio
    rs_momentum: list[float] = [0.0] * n
    for i in range(1, n):
        rs_momentum[i] = rs_ratio[i] - rs_ratio[i - 1]

    latest_ratio = rs_ratio[-1]
    latest_momentum = rs_momentum[-1]

    return rs_ratio, latest_ratio, latest_momentum


def _classify_quadrant(rs_ratio: float, rs_momentum: float) -> str:
    """Classify into RRG quadrant.

    - leading:   RS-Ratio > 0, RS-Momentum > 0
    - weakening: RS-Ratio > 0, RS-Momentum < 0
    - lagging:   RS-Ratio < 0, RS-Momentum < 0
    - improving: RS-Ratio < 0, RS-Momentum > 0
    """
    if rs_ratio > 0:
        return "leading" if rs_momentum > 0 else "weakening"
    else:
        return "improving" if rs_momentum > 0 else "lagging"


@dataclass(frozen=True)
class RelativeRotationResult:
    """Result of :func:`relative_rotation_report`."""

    per_asset: dict[str, dict[str, Any]]
    latest_quadrants: dict[str, str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_asset": self.per_asset,
            "latest_quadrants": self.latest_quadrants,
        }


def relative_rotation_report(
    assets: dict[str, list[float]],
    benchmark: list[float],
    *,
    tail: int = 10,
) -> RelativeRotationResult:
    """Compute Relative Rotation Graph (RRG) classification for multiple assets.

    Parameters
    ----------
    assets:
        Dict of ``{symbol: [prices]}``. At most 50 assets.
    benchmark:
        Benchmark price series. Must match the length of every asset series.
    tail:
        Rolling window for RS-Ratio z-score normalization.

    Returns
    -------
    RelativeRotationResult
        Per-asset ``rs_ratio``, ``rs_momentum``, and ``quadrant``, plus
        a ``latest_quadrants`` snapshot.

    Raises
    ------
    ValueError
        On any invalid input.
    """
    if isinstance(tail, bool) or not isinstance(tail, int):
        raise ValueError("tail must be an int")
    if tail < 2:
        raise ValueError("tail must be >= 2")

    bench = _validate_benchmark(benchmark, tail)
    asset_dict = _validate_assets(assets, len(bench))

    per_asset: dict[str, dict[str, Any]] = {}
    latest_quadrants: dict[str, str] = {}

    for name, prices in asset_dict.items():
        _rs_ratio, latest_ratio, latest_momentum = _compute_rs_ratio(
            prices, bench, tail
        )
        quadrant = _classify_quadrant(latest_ratio, latest_momentum)
        per_asset[name] = {
            "rs_ratio": latest_ratio,
            "rs_momentum": latest_momentum,
            "quadrant": quadrant,
        }
        latest_quadrants[name] = quadrant

    return RelativeRotationResult(
        per_asset=per_asset,
        latest_quadrants=latest_quadrants,
    )
