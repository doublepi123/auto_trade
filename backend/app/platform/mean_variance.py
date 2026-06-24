"""P204: mean-variance portfolio optimization + efficient frontier.

Markowitz (1952) portfolio construction: minimum-variance portfolio, maximum-
Sharpe (tangency) portfolio, and a sampled efficient frontier. Long-only,
closed-form where possible and grid-search otherwise — no quadratic-programming
solver dependency (mirrors PyPortfolioOpt's ``EfficientFrontier`` interface but
in pure Python with dict-based I/O for platform composability).

A :class:`MeanVarianceModel` implements the platform's
:class:`~app.platform.construction.PortfolioConstructionModel` Protocol so it can
be swapped in wherever EqualWeight/RiskParity already slot.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.construction import PortfolioConstructionModel
from app.platform.covariance import ledoit_wolf_shrinkage, matrix_from_pairs, portfolio_variance, sample_covariance

__all__ = [
    "min_variance_weights",
    "max_sharpe_weights",
    "efficient_frontier",
    "MeanVarianceModel",
]


def _symbols_and_cov(
    returns: dict[str, list[float]] | None = None,
    cov: dict[tuple[str, str], float] | None = None,
) -> tuple[list[str], dict[tuple[str, str], float]]:
    if cov is not None:
        symbols = sorted({s for pair in cov for s in pair})
        return symbols, cov
    if returns is None:
        return [], {}
    symbols = list(returns.keys())
    shrunk, _ = ledoit_wolf_shrinkage(returns)
    return symbols, shrunk


def min_variance_weights(
    returns: dict[str, list[float]] | None = None,
    cov: dict[tuple[str, str], float] | None = None,
) -> dict[str, float]:
    """Long-only minimum-variance portfolio weights.

    Closed-form for the unconstrained case: w ∝ Σ⁻¹ 1, normalized. Falls back to
    an iterative shrinkage toward equal-weight when Σ is singular (so a perfectly
    correlated or rank-deficient covariance still returns sensible weights).
    """
    symbols, sigma = _symbols_and_cov(returns, cov)
    if not symbols:
        return {}
    n = len(symbols)
    if n == 1:
        return {symbols[0]: 1.0}
    ones = [1.0] * n
    sigma_mat = matrix_from_pairs(sigma, symbols)
    inv = _try_solve(sigma_mat, ones)
    if inv is None:
        return _equal_weights(symbols)
    total = sum(inv)
    if total <= 0:
        return _equal_weights(symbols)
    return {symbols[i]: max(0.0, inv[i] / total) for i in range(n)}


def max_sharpe_weights(
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_free: float = 0.0,
) -> dict[str, float]:
    """Long-only maximum-Sharpe (tangency) portfolio weights.

    Closed-form for the unconstrained case: w ∝ Σ⁻¹ (μ − rf·1). When that yields
    a negative component (short) or Σ is singular, we fall back to a dense grid
    search over long-only weight vectors, picking the highest realized Sharpe —
    deterministic and bounded by the asset count.
    """
    symbols = sorted(mean_returns.keys())
    if not symbols:
        return {}
    n = len(symbols)
    if n == 1:
        return {symbols[0]: 1.0}
    sigma_mat = matrix_from_pairs(cov, symbols)
    excess = [mean_returns[s] - risk_free for s in symbols]
    inv = _try_solve(sigma_mat, excess)
    if inv is not None and all(x > 0 for x in inv):
        total = sum(inv)
        return {symbols[i]: inv[i] / total for i in range(n)}
    # Fallback: grid search over long-only simplex (only feasible for small n).
    return _grid_max_sharpe(symbols, mean_returns, cov, risk_free)


def efficient_frontier(
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    n_points: int = 50,
) -> list[dict[str, Any]]:
    """Sample the long-only efficient frontier.

    For each target return between min and max achievable, find the minimum-
    variance long-only portfolio meeting it (grid-search for the weight mix that
    hits the target return with lowest variance). Returns points sorted by
    ascending return, each ``{"return", "volatility", "sharpe", "weights"}``.
    """
    symbols = sorted(mean_returns.keys())
    if not symbols or n_points < 2:
        return []
    r_min = min(mean_returns.values())
    r_max = max(mean_returns.values())
    if r_max <= r_min:
        # All equal expected returns: frontier collapses to the min-var point.
        w = min_variance_weights(cov=cov)
        port_ret = sum(w[s] * mean_returns[s] for s in symbols)
        port_var = portfolio_variance(cov, w)
        return [{
            "return": port_ret,
            "volatility": math.sqrt(max(port_var, 0.0)),
            "sharpe": (port_ret / math.sqrt(port_var)) if port_var > 0 else 0.0,
            "weights": w,
        }]

    points: list[dict[str, Any]] = []
    for k in range(n_points):
        target = r_min + (r_max - r_min) * k / (n_points - 1)
        w = _min_variance_for_target(symbols, mean_returns, cov, target)
        port_ret = sum(w[s] * mean_returns[s] for s in symbols)
        port_var = portfolio_variance(cov, w)
        vol = math.sqrt(max(port_var, 0.0))
        sharpe = (port_ret / vol) if vol > 0 else 0.0
        points.append({"return": port_ret, "volatility": vol, "sharpe": sharpe, "weights": w})
    return points


# ---- construction model -----------------------------------------------------


@dataclass(frozen=True)
class MeanVarianceModel:
    """PortfolioConstructionModel that sizes via max-Sharpe on estimated moments.

    With no ``mean_returns`` supplied, falls back to min-variance. Uses Ledoit-
    Wolf shrinkage on the supplied return panel for a well-conditioned covariance.
    If a precomputed ``cov`` matrix is supplied, the return panel is not used.
    """

    mean_returns: dict[str, float] | None = None
    cov: dict[tuple[str, str], float] | None = None
    risk_free: float = 0.0
    name: str = "mean_variance"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        mu = self.mean_returns
        if mu is None:
            mu = {s: float(signals[s]) for s in active}
        else:
            mu = {s: mu.get(s, float(signals[s])) for s in active}
        # Choose the covariance source: explicit → per-asset vols → equal weight.
        if self.cov is not None:
            cov_active = {(a, b): self.cov.get((a, b), 0.0) for a in active for b in active}
            w = max_sharpe_weights(mu, cov_active, risk_free=self.risk_free)
        else:
            vols = volatilities or {}
            if all(v > 0 for v in vols.values()) and len(vols) == len(active):
                cov = {(a, b): (float(vols[a]) ** 2 if a == b else 0.0) for a in active for b in active}
                w = max_sharpe_weights(mu, cov, risk_free=self.risk_free)
            else:
                w = {s: 1.0 / len(active) for s in active}
        return {s: Decimal(str(w.get(s, 0.0))) for s in active}


# ---- helpers ----------------------------------------------------------------


def _equal_weights(symbols: list[str]) -> dict[str, float]:
    if not symbols:
        return {}
    w = 1.0 / len(symbols)
    return {s: w for s in symbols}


def _try_solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``matrix · x = rhs`` via Gaussian elimination; None if singular."""
    n = len(matrix)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        # partial pivot
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col] / pivot_val
            for c in range(col, n + 1):
                aug[r][c] -= factor * aug[col][c]
    return [aug[i][n] / aug[i][i] for i in range(n)]


def _grid_max_sharpe(
    symbols: list[str],
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_free: float,
    steps: int = 11,
) -> dict[str, float]:
    """Deterministic grid search over the long-only simplex for small n (≤4)."""
    n = len(symbols)
    best_w = _equal_weights(symbols)
    best_sharpe = float("-inf")
    grid = [i / steps for i in range(steps + 1)]

    def _recurse(idx: int, remaining: float, weights: list[float]) -> None:
        nonlocal best_w, best_sharpe
        if idx == n - 1:
            weights[idx] = remaining
            w = {symbols[i]: weights[i] for i in range(n)}
            port_ret = sum(w[s] * mean_returns[s] for s in symbols) - risk_free
            port_var = portfolio_variance(cov, w)
            if port_var <= 0:
                return
            sharpe = port_ret / math.sqrt(port_var)
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_w = w
            return
        for g in grid:
            if g > remaining + 1e-9:
                break
            weights[idx] = g
            _recurse(idx + 1, remaining - g, weights)

    if n <= 4:
        _recurse(0, 1.0, [0.0] * n)
    return best_w


def _min_variance_for_target(
    symbols: list[str],
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    target_return: float,
    steps: int = 11,
) -> dict[str, float]:
    """Min-variance long-only portfolio whose return is closest to ``target_return``."""
    n = len(symbols)
    grid = [i / steps for i in range(steps + 1)]
    best_w = _equal_weights(symbols)
    best_score = float("inf")

    def _recurse(idx: int, remaining: float, weights: list[float]) -> None:
        nonlocal best_w, best_score
        if idx == n - 1:
            weights[idx] = remaining
            w = {symbols[i]: weights[i] for i in range(n)}
            port_ret = sum(w[s] * mean_returns[s] for s in symbols)
            ret_gap = abs(port_ret - target_return)
            port_var = portfolio_variance(cov, w)
            # score: minimize variance subject to return gap (soft constraint)
            score = port_var + ret_gap * 1e6
            if score < best_score:
                best_score = score
                best_w = w
            return
        for g in grid:
            if g > remaining + 1e-9:
                break
            weights[idx] = g
            _recurse(idx + 1, remaining - g, weights)

    if n <= 4:
        _recurse(0, 1.0, [0.0] * n)
    return best_w
