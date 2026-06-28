"""P333: Dynamic style analysis — rolling-window factor exposure regression.

Performs style regressions on a rolling-window basis to detect shifts in a
strategy's factor exposures over time (style drift). Reuses the NNLS / NNLS-simplex
solvers from ``style_analysis.py`` for each window regression.

Three constraint modes:
* ``"sum_eq_one"`` — non-negative weights summing to 1
* ``"sum_le_one"`` — non-negative weights summing to ≤ 1 (cash residual)
* ``"none"`` — unconstrained OLS (can have negative weights)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

from app.platform.style_analysis import nnls, nnls_simplex

__all__ = ["DynamicStyleResult", "dynamic_style_analysis_report"]


@dataclass(frozen=True)
class DynamicStyleResult:
    per_window_weights: list[dict[str, float]] = field(default_factory=list)
    r_squared_series: list[float] = field(default_factory=list)
    style_drift_score: float = 0.0
    drift_detected: bool = False
    drift_threshold: float = 0.05

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_window_weights": self.per_window_weights,
            "r_squared_series": self.r_squared_series,
            "style_drift_score": self.style_drift_score,
            "drift_detected": self.drift_detected,
            "drift_threshold": self.drift_threshold,
        }


def _ols_solve(A: list[list[float]], b: list[float]) -> list[float] | None:
    """Unconstrained OLS via normal equations (AtA)^-1 Atb. Returns None if singular."""
    n = len(A)
    if n == 0:
        return None
    k = len(A[0])
    if k == 0:
        return None
    AtA = [[sum(A[t][i] * A[t][j] for t in range(n)) for j in range(k)] for i in range(k)]
    Atb = [sum(A[t][i] * b[t] for t in range(n)) for i in range(k)]

    # Gaussian elimination on AtA
    m = k
    aug = [row[:] + [Atb[i]] for i, row in enumerate(AtA)]
    for col in range(m):
        pivot = max(range(col, m), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pv = aug[col][col]
        for r in range(m):
            if r == col:
                continue
            factor = aug[r][col] / pv
            for c in range(col, m + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][m] / aug[i][i] for i in range(m)]


def dynamic_style_analysis_report(
    returns: list[float],
    factor_returns: dict[str, list[float]],
    *,
    window: int = 20,
    constraint: str = "sum_eq_one",
) -> DynamicStyleResult:
    """Rolling-window dynamic style analysis.

    For each trailing window of length ``window``, regress the strategy returns
    against the factor returns using the specified constraint, collecting per-window
    factor weights and R² values. Then computes a style drift score (mean variance
    of weights over time) and flags drift if the score exceeds the default threshold.

    Args:
        returns: Strategy return series.
        factor_returns: {factor_name: return_series} panel. All series must be
            aligned and longer than ``window``.
        window: Sliding window length (must be ≥ 2).
        constraint: One of ``"sum_eq_one"``, ``"sum_le_one"``, ``"none"``.

    Returns:
        DynamicStyleResult with per-window weights, R² series, drift score, and flag.

    Raises:
        ValueError: If inputs are invalid (empty, short, mismatched lengths, etc.).
    """
    if constraint not in ("sum_eq_one", "sum_le_one", "none"):
        raise ValueError(f"unknown constraint: {constraint}")
    if not returns or not isinstance(returns, list):
        raise ValueError("returns must be a non-empty list")
    if not factor_returns or not isinstance(factor_returns, dict):
        raise ValueError("factor_returns must be a non-empty dict")
    if not isinstance(window, int) or window < 2:
        raise ValueError("window must be an integer >= 2")

    for v in returns:
        if not math.isfinite(v):
            raise ValueError("returns must contain only finite numbers")
    factors = list(factor_returns.keys())
    expected_len = len(returns)
    for f in factors:
        if len(factor_returns[f]) != expected_len:
            raise ValueError("returns and factor_returns must have equal length")
    n_common = expected_len
    if n_common < window:
        raise ValueError(f"need at least {window} aligned observations, got {n_common}")
    for f in factors:
        for v in factor_returns[f]:
            if not math.isfinite(v):
                raise ValueError(f"factor '{f}' must contain only finite numbers")

    returns_aligned = [float(returns[i]) for i in range(n_common)]
    factor_cols = {f: [float(factor_returns[f][i]) for i in range(n_common)] for f in factors}

    per_window_weights: list[dict[str, float]] = []
    r_squared_series: list[float] = []

    for end in range(window - 1, n_common):
        start = end - window + 1
        b = returns_aligned[start : end + 1]
        A = [[factor_cols[f][i] for f in factors] for i in range(start, end + 1)]

        if constraint == "sum_eq_one":
            x = nnls_simplex(A, b)
        elif constraint == "sum_le_one":
            x = nnls(A, b)
            if sum(x) > 1.0 + 1e-9:
                x = nnls_simplex(A, b)
        else:  # none (unconstrained OLS)
            x_ols = _ols_solve(A, b)
            if x_ols is None:
                x = [0.0] * len(factors)
            else:
                x = x_ols

        # Clip tiny negatives from float arithmetic
        x = [max(0.0, v) if v > -1e-9 else v for v in x]

        weights = {factors[j]: x[j] for j in range(len(factors))}
        per_window_weights.append(weights)

        # Compute R² for this window
        fitted = [sum(x[j] * A[t][j] for j in range(len(factors))) for t in range(window)]
        residual = [b[t] - fitted[t] for t in range(window)]
        rss = sum(r * r for r in residual)
        mean_b = sum(b) / window
        tss = sum((v - mean_b) ** 2 for v in b)
        if tss <= 1e-18 and rss <= 1e-18:
            r_squared = 1.0
        elif tss <= 1e-18:
            r_squared = 0.0
        else:
            r_squared = max(0.0, min(1.0, 1.0 - rss / tss))
        r_squared_series.append(r_squared)

    # Style drift score: mean of per-factor variance over time
    if factors and per_window_weights:
        n_windows = len(per_window_weights)
        drift_score = 0.0
        for f in factors:
            vals = [w.get(f, 0.0) for w in per_window_weights]
            mean_val = sum(vals) / n_windows
            var = sum((v - mean_val) ** 2 for v in vals) / n_windows
            drift_score += var
        drift_score /= len(factors)
    else:
        drift_score = 0.0

    drift_threshold = 0.05
    drift_detected = drift_score > drift_threshold

    return DynamicStyleResult(
        per_window_weights=per_window_weights,
        r_squared_series=r_squared_series,
        style_drift_score=drift_score,
        drift_detected=drift_detected,
        drift_threshold=drift_threshold,
    )
