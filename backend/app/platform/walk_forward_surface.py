"""P328: Walk-forward surface — rolling IS/OOS Sharpe degradation.

Train-test rolling evaluation of a return series: for each overlapping
train/test window pair, compute IS Sharpe (training) and OOS Sharpe (test),
plus the degradation (IS − OOS). Aggregates into a per-segment surface
and summary statistics.

Reference: Bailey et al. (2017) "The Probability of Backtest Overfitting";
Aronson "Evidence-Based Technical Analysis" Ch.8. Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any


_ZERO_VARIANCE_TOL = 1e-30


@dataclass(frozen=True)
class WalkForwardSegment:
    start_idx: int
    end_idx: int
    is_sharpe: float
    oos_sharpe: float
    degradation: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "start_idx": self.start_idx,
            "end_idx": self.end_idx,
            "is_sharpe": self.is_sharpe,
            "oos_sharpe": self.oos_sharpe,
            "degradation": self.degradation,
        }


@dataclass(frozen=True)
class WalkForwardSurfaceResult:
    n_observations: int
    train_window: int
    test_window: int
    segments: list[WalkForwardSegment]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "n_observations": self.n_observations,
            "train_window": self.train_window,
            "test_window": self.test_window,
            "segments": [s.to_dict() for s in self.segments],
            "summary": self.summary,
        }


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _median(xs: list[float]) -> float:
    """Standard median: middle element (odd) or average of two middle (even)."""
    n = len(xs)
    if n == 0:
        return 0.0
    s = sorted(xs)
    mid = n // 2
    if n % 2 == 1:
        return s[mid]
    return (s[mid - 1] + s[mid]) / 2.0


def _std(xs: list[float]) -> float:
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    variance = sum((x - m) ** 2 for x in xs) / (n - 1)
    if variance <= _ZERO_VARIANCE_TOL:
        return 0.0
    return math.sqrt(variance)


def _sharpe(returns: list[float], periods_per_year: int = 1) -> float:
    """Annualized Sharpe ratio from period returns."""
    n = len(returns)
    if n < 2:
        return 0.0
    mu = _mean(returns)
    sigma = _std(returns)
    if sigma <= 0:
        return 0.0
    return (mu / sigma) * math.sqrt(periods_per_year)


def walk_forward_surface_report(
    returns: list[float],
    *,
    train_window: int = 20,
    test_window: int = 10,
) -> WalkForwardSurfaceResult:
    """Rolling IS/OOS Sharpe degradation surface.

    For each rolling train+test window pair:
    - IS Sharpe: computed on training data
    - OOS Sharpe: computed on test data
    - degradation: IS − OOS (positive = overfitting signal)

    Args:
        returns: Period returns list.
        train_window: Number of periods in training window.
        test_window: Number of periods in test window.

    Returns:
        WalkForwardSurfaceResult with per-segment IS/OOS Sharpes and summary.

    Raises:
        ValueError: Empty returns, invalid windows, or non-finite values.
    """
    if not returns:
        raise ValueError("returns must be non-empty")
    if train_window < 1:
        raise ValueError("train_window must be >= 1")
    if test_window < 1:
        raise ValueError("test_window must be >= 1")

    returns_f = []
    for v in returns:
        fv = float(v)
        if not math.isfinite(fv):
            raise ValueError("returns must contain only finite numbers")
        returns_f.append(fv)

    n = len(returns_f)
    step = test_window  # non-overlapping test windows; train rolls with step
    segments: list[WalkForwardSegment] = []

    # Walk forward: for each contiguous train+test block
    start = 0
    while start + train_window + test_window <= n:
        train_data = returns_f[start : start + train_window]
        test_start = start + train_window
        test_data = returns_f[test_start : test_start + test_window]

        is_s = _sharpe(train_data)
        oos_s = _sharpe(test_data)
        degradation = is_s - oos_s

        segments.append(WalkForwardSegment(
            start_idx=start,
            end_idx=test_start + test_window,
            is_sharpe=is_s,
            oos_sharpe=oos_s,
            degradation=degradation,
        ))

        start += step

    # Summary
    if segments:
        degradations = [s.degradation for s in segments]
        is_sharpes = [s.is_sharpe for s in segments]
        oos_sharpes = [s.oos_sharpe for s in segments]
        summary = {
            "n_segments": len(segments),
            "mean_degradation": _mean(degradations),
            "median_degradation": _median(degradations),
            "mean_is_sharpe": _mean(is_sharpes),
            "mean_oos_sharpe": _mean(oos_sharpes),
            "min_degradation": min(degradations),
            "max_degradation": max(degradations),
        }
    else:
        summary = {
            "n_segments": 0,
            "mean_degradation": 0.0,
            "median_degradation": 0.0,
            "mean_is_sharpe": 0.0,
            "mean_oos_sharpe": 0.0,
            "min_degradation": 0.0,
            "max_degradation": 0.0,
        }

    return WalkForwardSurfaceResult(
        n_observations=n,
        train_window=train_window,
        test_window=test_window,
        segments=segments,
        summary=summary,
    )


__all__ = [
    "WalkForwardSegment",
    "WalkForwardSurfaceResult",
    "walk_forward_surface_report",
]
