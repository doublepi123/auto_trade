"""P248: Robust statistics — estimators that resist outliers.

A dependency-free toolkit of the classical robust estimators, mirroring the
shape of ``statsmodels.robust`` but in pure Python:

* **mad** — median absolute deviation ``median(|x − median(x)|)``; the
  consistent normal estimator scales by 1.4826 (``Φ⁻¹(0.75)``).
* **winsorize** — clip values to the ``[α, 1−α]`` quantile brackets
  (empirical quantile via linear interpolation).
* **trimmed_mean** — α-trimmed mean: discard the smallest and largest
  ``α·n`` values, average the rest.
* **theil_sen** — Theil (1950) / Sen (1968) median-of-slopes regression
  slope; robust to up to ~29% outliers. O(n²) pairwise slopes.
* **huber** — Huber (1964) M-estimator of location via iteratively
  reweighted least squares (IRLS) with a configurable k and max iterations.

All deterministic (no RNG). Pure Python, no scipy/numpy.

Reference: Huber (1964) "Robust Estimation of a Location Parameter";
Theil (1950), Sen (1968); Tukey-McLaughlin winsorization; Hampel et al.
"Robust Statistics: The Approach Based on Influence Functions".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Sequence

__all__ = [
    "RobustStatsResult",
    "mad",
    "winsorize",
    "trimmed_mean",
    "theil_sen",
    "huber",
    "robust_stats",
]


def _median(sorted_xs: list[float]) -> float:
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("empty input")
    mid = n // 2
    if n % 2 == 1:
        return sorted_xs[mid]
    return 0.5 * (sorted_xs[mid - 1] + sorted_xs[mid])


def _quantile(sorted_xs: list[float], q: float) -> float:
    """Linear-interpolation quantile of an already-sorted list."""
    n = len(sorted_xs)
    if n == 0:
        raise ValueError("empty input")
    if q <= 0.0:
        return sorted_xs[0]
    if q >= 1.0:
        return sorted_xs[-1]
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    frac = pos - lo
    return sorted_xs[lo] * (1.0 - frac) + sorted_xs[hi] * frac


def mad(xs: Sequence[float], *, normalize: bool = True) -> float:
    """Median absolute deviation; scaled by 1.4826 when ``normalize``."""
    if not xs:
        raise ValueError("xs must be non-empty")
    s = sorted(float(x) for x in xs)
    med = _median(s)
    devs = sorted(abs(x - med) for x in xs)
    m = _median(devs)
    return m * 1.482602218505602 if normalize else m


def winsorize(xs: Sequence[float], alpha: float = 0.05) -> list[float]:
    """Clip ``xs`` to the ``[α, 1−α]`` empirical quantiles."""
    if not 0.0 <= alpha < 0.5:
        raise ValueError("alpha must be in [0, 0.5)")
    if not xs:
        return []
    s = sorted(float(x) for x in xs)
    lo = _quantile(s, alpha)
    hi = _quantile(s, 1.0 - alpha)
    return [min(max(x, lo), hi) for x in xs]


def trimmed_mean(xs: Sequence[float], alpha: float = 0.1) -> float:
    """α-trimmed mean: drop the smallest/largest ``α·n`` and average the rest."""
    if not 0.0 <= alpha < 0.5:
        raise ValueError("alpha must be in [0, 0.5)")
    n = len(xs)
    if n == 0:
        raise ValueError("xs must be non-empty")
    s = sorted(float(x) for x in xs)
    k = int(math.floor(alpha * n))
    kept = s[k:n - k] if (n - 2 * k) > 0 else s
    if not kept:
        return _median(s)
    return sum(kept) / len(kept)


def theil_sen(y: Sequence[float], x: Sequence[float]) -> tuple[float, float]:
    """Theil-Sen median-of-slopes regression; returns ``(slope, intercept)``.

    The intercept is ``median(y) − slope · median(x)``. Robust to up to
    ~29.3% contaminated points. Raises ``ValueError`` on length mismatch /
    empty / insufficient distinct x.
    """
    n = len(y)
    if n != len(x):
        raise ValueError("y and x must have equal length")
    if n < 2:
        raise ValueError("need at least 2 points")
    slopes: list[float] = []
    for i in range(n):
        xi = float(x[i])
        yi = float(y[i])
        for j in range(i + 1, n):
            dx = float(x[j]) - xi
            if dx == 0.0:
                continue
            slopes.append((float(y[j]) - yi) / dx)
    if not slopes:
        raise ValueError("all x values identical; slope undefined")
    slope = _median(sorted(slopes))
    med_x = _median(sorted(float(v) for v in x))
    med_y = _median(sorted(float(v) for v in y))
    intercept = med_y - slope * med_x
    return slope, intercept


def huber(xs: Sequence[float], k: float = 1.345, *, max_iter: int = 50, tol: float = 1e-8) -> float:
    """Huber M-estimator of location via IRLS.

    The Huber loss ``ρ(u) = ½u² for |u|≤k, k|u|−½k² otherwise`` yields weights
    ``w(u) = min(1, k/|u|)``; we iterate the weighted mean until convergence
    with a MAD-based scale estimate. Raises ``ValueError`` on empty input /
    non-positive k / zero scale.
    """
    if not xs:
        raise ValueError("xs must be non-empty")
    if k <= 0.0:
        raise ValueError("k must be positive")
    s = [float(x) for x in xs]
    mu = _median(sorted(s))
    scale = mad(s)
    if scale <= 0.0:
        # All values identical (or near); return the median.
        return mu
    for _ in range(max_iter):
        weights = [min(1.0, k / (abs(x - mu) / scale)) if (x - mu) != 0.0 else 1.0 for x in s]
        sw = sum(weights)
        if sw == 0.0:
            break
        new_mu = sum(w * x for w, x in zip(weights, s)) / sw
        if abs(new_mu - mu) < tol * max(abs(mu), 1.0):
            mu = new_mu
            break
        mu = new_mu
    return mu


@dataclass(frozen=True)
class RobustStatsResult:
    median: float
    mad: float
    trimmed_mean: float
    winsorized_mean: float
    theil_sen_slope: float | None
    theil_sen_intercept: float | None
    huber_location: float

    def to_dict(self) -> dict:
        return {
            "median": self.median,
            "mad": self.mad,
            "trimmed_mean": self.trimmed_mean,
            "winsorized_mean": self.winsorized_mean,
            "theil_sen_slope": self.theil_sen_slope,
            "theil_sen_intercept": self.theil_sen_intercept,
            "huber_location": self.huber_location,
        }


def robust_stats(
    xs: Sequence[float],
    y: Sequence[float] | None = None,
    x: Sequence[float] | None = None,
    *,
    alpha: float = 0.1,
    huber_k: float = 1.345,
) -> RobustStatsResult:
    """Aggregate robust location/scale estimators for ``xs``.

    If ``y`` and ``x`` are supplied, the Theil-Sen slope/intercept are
    computed; otherwise they are ``None``. Raises ``ValueError`` on empty
    input or invalid parameters.
    """
    if not xs:
        raise ValueError("xs must be non-empty")
    s = sorted(float(v) for v in xs)
    med = _median(s)
    mad_v = mad(xs)
    tm = trimmed_mean(xs, alpha)
    wm = sum(winsorize(xs, alpha)) / len(xs)
    if y is not None and x is not None:
        slope, intercept = theil_sen(y, x)
        ts_slope: float | None = slope
        ts_int: float | None = intercept
    else:
        ts_slope = None
        ts_int = None
    loc = huber(xs, k=huber_k)
    return RobustStatsResult(
        median=med,
        mad=mad_v,
        trimmed_mean=tm,
        winsorized_mean=wm,
        theil_sen_slope=ts_slope,
        theil_sen_intercept=ts_int,
        huber_location=loc,
    )