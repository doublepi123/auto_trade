"""P322: Spread Stability — rolling-window hedge ratio + half-life diagnostics.

Estimates a rolling-window regression of ``y ~ x`` to track hedge-ratio
stability, computes the mean-reversion half-life of the residual spread,
and flags breakdowns (abrupt changes in half-life).

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


__all__ = ["SpreadStabilityResult", "spread_stability_report"]


@dataclass(frozen=True)
class SpreadStabilityResult:
    """Frozen result of :func:`spread_stability_report`.

    * ``hedge_ratios`` — rolling-window OLS slope of ``y ~ x`` at each
      window endpoint (``None`` for indices before the first full window).
    * ``half_lives`` — rolling half-life of the residual spread (OU
      mean-reversion), same length as ``hedge_ratios``.
    * ``breakdown_flags`` — boolean flags where the half-life jumps by more
      than 3× its local standard deviation.
    """

    hedge_ratios: list[float | None]
    half_lives: list[float | None]
    breakdown_flags: list[bool]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hedge_ratios": self.hedge_ratios,
            "half_lives": self.half_lives,
            "breakdown_flags": self.breakdown_flags,
        }


def _validate_series(y: list[float], x: list[float], window: int) -> None:
    """Raise ValueError for any invalid inputs."""
    if len(y) != len(x):
        raise ValueError("y and x must have equal length")
    if len(y) < window:
        raise ValueError(f"series length ({len(y)}) must be >= window ({window})")
    if window < 3:
        raise ValueError("window must be >= 3")
    for i, (yi, xi) in enumerate(zip(y, x)):
        if not math.isfinite(yi) or not math.isfinite(xi):
            raise ValueError(f"y and x must be finite at index {i}")


def _ols_slope(ys: list[float], xs: list[float]) -> float | None:
    """OLS slope of ``ys ~ xs``. Returns None if xs is constant."""
    n = len(ys)
    mean_y = sum(ys) / n
    mean_x = sum(xs) / n
    num = 0.0
    den = 0.0
    for yi, xi in zip(ys, xs):
        num += (yi - mean_y) * (xi - mean_x)
        den += (xi - mean_x) ** 2
    if den < 1e-15:
        return None
    return num / den


def _half_life_from_residuals(residuals: list[float]) -> float | None:
    """Estimate OU half-life from a residual series.

    Regresses Δr on r_{t-1}:  Δr_t = κ * r_{t-1} + ε_t.
    Half-life = ln(2) / |κ|.
    """
    n = len(residuals)
    if n < 2:
        return None
    ys = [residuals[i] - residuals[i - 1] for i in range(1, n)]
    xs = residuals[:-1]
    kappa = _ols_slope(ys, xs)
    if kappa is None or kappa >= 0.0:
        return None
    return math.log(2.0) / abs(kappa)


def spread_stability_report(
    y: list[float], x: list[float], *, window: int = 20
) -> SpreadStabilityResult:
    """Compute rolling hedge-ratio stability diagnostics.

    Parameters
    ----------
    y : list[float]
        Dependent series (typically the "spread" asset).
    x : list[float]
        Independent series (the "hedge" asset).
    window : int
        Rolling window size (default 20).

    Returns
    -------
    SpreadStabilityResult
    """
    _validate_series(y, x, window)
    n = len(y)

    hedge_ratios: list[float | None] = [None] * (window - 1)
    half_lives: list[float | None] = [None] * (window - 1)
    spreads_residuals: list[float] = []

    for i in range(window - 1, n):
        ys = y[i - window + 1 : i + 1]
        xs = x[i - window + 1 : i + 1]
        hr = _ols_slope(ys, xs)
        hedge_ratios.append(hr)
        if hr is not None:
            residuals = [ys[j] - hr * xs[j] for j in range(window)]
            spreads_residuals.append(residuals[-1])
            hl = _half_life_from_residuals(residuals)
            half_lives.append(hl)
        else:
            half_lives.append(None)

    # Breakdown flags: detect jumps > 3× local std of half-life
    breakdown_flags: list[bool] = [False] * len(half_lives)
    valid_hls = [(i, hl) for i, hl in enumerate(half_lives) if hl is not None]
    if len(valid_hls) >= 2:
        hls = [hl for _, hl in valid_hls]
        mean_hl = sum(hls) / len(hls)
        sd_hl = math.sqrt(sum((v - mean_hl) ** 2 for v in hls) / len(hls))
        if sd_hl > 1e-15:
            for i, hl in valid_hls:
                if abs(hl - mean_hl) > 3.0 * sd_hl:
                    breakdown_flags[i] = True

    return SpreadStabilityResult(
        hedge_ratios=hedge_ratios,
        half_lives=half_lives,
        breakdown_flags=breakdown_flags,
    )
