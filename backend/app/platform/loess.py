"""P250: LOESS / LOWESS locally-weighted regression.

Cleveland's (1979) LOESS (LOcal regrESSion) and LOWESS (LOcally WEighted
Scatterplot Smoother): at each evaluation point ``x₀``, fit a local
(usually linear) regression weighted by the *tricube* kernel

    T(u) = (1 − |u|³)³   for |u| ≤ 1, 0 otherwise

with bandwidth ``f`` (fraction of points in the neighbourhood). An optional
robustification step (Cleveland 1979) re-weights points by their residual
magnitude across ``iter`` iterations, down-weighting outliers, mirroring
``statsmodels.nonparametric.lowess``.

Pure Python, no numpy/scipy. Local linear regression is solved by weighted
normal equations (2×2 system). Deterministic.

Reference: Cleveland (1979) "Robust Locally Weighted Regression and Smoothing
Scatterplots"; Cleveland & Devlin (1988); statsmodels.lowess.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

__all__ = ["LowessResult", "lowess"]


def _solve2(a: list[list[float]], b: list[float]) -> list[float] | None:
    """Solve a 2×2 linear system; returns None if singular."""
    det = a[0][0] * a[1][1] - a[0][1] * a[1][0]
    if abs(det) < 1e-18:
        return None
    inv = 1.0 / det
    x0 = (a[1][1] * b[0] - a[0][1] * b[1]) * inv
    x1 = (-a[1][0] * b[0] + a[0][0] * b[1]) * inv
    return [x0, x1]


def _tricube(u: float) -> float:
    if abs(u) >= 1.0:
        return 0.0
    v = 1.0 - abs(u) ** 3
    return v * v * v


@dataclass(frozen=True)
class LowessResult:
    x: list[float]
    y: list[float]
    smoothed: list[float]
    n_points: int
    bandwidth: float
    iterations: int

    def to_dict(self) -> dict:
        return {
            "x": self.x,
            "y": self.y,
            "smoothed": self.smoothed,
            "n_points": self.n_points,
            "bandwidth": self.bandwidth,
            "iterations": self.iterations,
        }


def lowess(
    x: Sequence[float],
    y: Sequence[float],
    *,
    bandwidth: float = 0.3,
    iterations: int = 2,
) -> LowessResult:
    """Locally-weighted linear regression (LOWESS) with robust reweighting.

    Parameters
    ----------
    x, y : paired data (must be equal length ≥ 2).
    bandwidth : fraction ``f ∈ (0, 1]`` of points in each local neighbourhood.
    iterations : number of robustification iterations (0 = no robust step).

    Returns :class:`LowessResult` with ``smoothed`` aligned to ``x``. The
    returned points are sorted by ``x``. Raises ``ValueError`` on length
    mismatch / empty / invalid bandwidth.
    """
    n = len(x)
    if n != len(y):
        raise ValueError("x and y must have equal length")
    if n < 2:
        raise ValueError("need at least 2 points")
    if not 0.0 < bandwidth <= 1.0:
        raise ValueError("bandwidth must be in (0, 1]")
    if iterations < 0:
        raise ValueError("iterations must be non-negative")

    # Sort by x for neighbourhood lookup.
    order = sorted(range(n), key=lambda i: x[i])
    xs = [float(x[i]) for i in order]
    ys = [float(y[i]) for i in order]
    k = max(int(round(bandwidth * n)), 2)
    if k > n:
        k = n

    robust_weights = [1.0] * n
    smoothed = [0.0] * n

    def fit_one(target_idx: int) -> float:
        x0 = xs[target_idx]
        # k nearest neighbours by distance to x0.
        dists = [(abs(xs[i] - x0), i) for i in range(n)]
        dists.sort(key=lambda t: t[0])
        neighbours = dists[:k]
        # Scale by the k-th distance (max within the window).
        h = neighbours[-1][0]
        if h == 0.0:
            # All neighbours identical to x0 -> return the (robust-weighted) mean.
            rw = [robust_weights[i] for _, i in neighbours]
            sw = sum(rw)
            if sw == 0.0:
                return ys[target_idx]
            return sum(rw_j * ys[i] for (_, i), rw_j in zip(neighbours, rw)) / sw

        def weighted_fit(rw_use: list[float]) -> float:
            # Weighted linear regression: minimise Σ w_i (y_i − a − b x_i)².
            sxx = sxy = sx = sy = sw_total = 0.0
            for (d, i), rw_j in zip(neighbours, rw_use):
                t = _tricube(d / h) * rw_j
                if t == 0.0:
                    continue
                xi = xs[i]
                yi = ys[i]
                sw_total += t
                sx += t * xi
                sy += t * yi
                sxx += t * xi * xi
                sxy += t * xi * yi
            if sw_total == 0.0:
                return None  # type: ignore[return-value]
            mat = [[sxx, sx], [sx, sw_total]]
            rhs = [sxy, sy]
            sol = _solve2(mat, rhs)
            if sol is None:
                return sy / sw_total
            b, a = sol  # y ≈ a + b x
            return a + b * x0

        rw = [robust_weights[i] for _, i in neighbours]
        result = weighted_fit(rw)
        if result is not None:
            return result
        # All robust weights zeroed out the window -> fall back to the
        # non-robust (tricube-only) local fit, which is more stable than the
        # raw observation at the target.
        result = weighted_fit([1.0] * len(neighbours))
        if result is not None:
            return result
        return ys[target_idx]

    for _ in range(max(iterations, 1)):
        for i in range(n):
            smoothed[i] = fit_one(i)
        if iterations == 0:
            break
        # Robust step: re-weight by bisquare of scaled residuals.
        residuals = [ys[i] - smoothed[i] for i in range(n)]
        abs_res = sorted(abs(r) for r in residuals)
        # Median absolute residual.
        m = abs_res[n // 2] if n % 2 == 1 else 0.5 * (abs_res[n // 2 - 1] + abs_res[n // 2])
        scale = 6.0 * m if m > 0.0 else 0.0
        if scale > 0.0:
            for i in range(n):
                u = residuals[i] / scale
                robust_weights[i] = (1.0 - u * u) ** 2 if abs(u) < 1.0 else 0.0
        else:
            robust_weights = [1.0] * n

    # Re-map smoothed back to the original (unsorted) order.
    out = [0.0] * n
    for pos, orig in enumerate(order):
        out[orig] = smoothed[pos]
    return LowessResult(
        x=[float(v) for v in x],
        y=[float(v) for v in y],
        smoothed=out,
        n_points=n,
        bandwidth=bandwidth,
        iterations=iterations,
    )