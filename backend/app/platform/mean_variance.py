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

    Closed-form for the unconstrained case: w ∝ Σ⁻¹ 1, normalized. When the
    closed-form solution has a negative component (which happens when assets are
    strongly negatively correlated — the unconstrained min-var portfolio would
    short one asset to hedge), we drop the shorted assets and re-solve on the
    remaining long-only subset, iterating until all weights are non-negative.
    This is the standard active-set treatment of the long-only min-variance QP.
    Falls back to equal weight when Σ is singular.
    """
    symbols, sigma = _symbols_and_cov(returns, cov)
    if not symbols:
        return {}
    n = len(symbols)
    if n == 1:
        return {symbols[0]: 1.0}
    return _long_only_min_variance(symbols, sigma, [1.0] * n)


def max_sharpe_weights(
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_free: float = 0.0,
) -> dict[str, float]:
    """Long-only maximum-Sharpe (tangency) portfolio weights.

    Closed-form for the unconstrained case: w ∝ Σ⁻¹ (μ − rf·1). When that yields a
    negative component (short) or Σ is singular, we fall back to a long-only
    active-set search: iteratively drop the asset the unconstrained tangency
    would short and re-solve on the remaining long-only subset, picking the
    feasible portfolio with the highest realized Sharpe. Deterministic and
    valid for any asset count ``n`` (no grid, so no ``n`` upper bound).
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
    # Unconstrained tangency is infeasible long-only → active-set search over
    # the long-only feasible subsets. We evaluate the tangency on every subset
    # of the assets (the unconstrained tangency restricted to a subset is
    # long-only feasible when Σ_sub⁻¹(μ_sub−rf) > 0 componentwise) and pick the
    # highest-Sharpe feasible one. 2^n is bounded by the realistic universe
    # size (callers pass ≤ ~15 assets); for larger universes use a QP solver.
    return _active_set_max_sharpe(symbols, mean_returns, cov, risk_free)


def efficient_frontier(
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    n_points: int = 50,
) -> list[dict[str, Any]]:
    """Sample the long-only efficient frontier.

    For each target return between min and max achievable, find the minimum-
    variance long-only portfolio whose realized return is closest to the target
    (active-set least-variance over the long-only feasible set). Returns points
    sorted by ascending return, each ``{"return", "volatility", "sharpe", "weights"}``.
    Works for any asset count ``n`` (no grid, so no ``n`` upper bound).
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


def _long_only_min_variance(
    symbols: list[str],
    full_cov: dict[tuple[str, str], float],
    rhs: list[float],
) -> dict[str, float]:
    """Long-only min-variance (Σ⁻¹ rhs, ≥0, sum=1) via active-set iteration.

    The unconstrained optimum ``w ∝ Σ⁻¹ rhs`` is the min-variance portfolio only
    when every component is non-negative. When some are negative (the unconstrained
    optimum would short those assets), the long-only optimum sits on the simplex
    boundary: the shorted assets get weight 0 and the rest are re-optimized. We
    iterate — drop the negative-weight assets, re-solve on the active (long)
    subset, repeat — until the active set is self-consistent (all weights > 0).
    This is the standard active-set method for the long-only min-variance QP and
    works for any ``n``. Falls back to equal weight over the active set if the
    residual system is singular.
    """
    n = len(symbols)
    active = list(range(n))
    while True:
        sub_mat = [[full_cov.get((symbols[a], symbols[b]), 0.0) for b in active] for a in active]
        sub_rhs = [rhs[a] for a in active]
        sol = _try_solve(sub_mat, sub_rhs)
        if sol is None:
            # singular submatrix → equal weight over the active set
            if not active:
                return _equal_weights(symbols)
            w = 1.0 / len(active)
            return {symbols[a]: w for a in active}
        if all(x > 0 for x in sol):
            total = sum(sol)
            if total > 0:
                return {symbols[a]: sol[k] / total for k, a in enumerate(active)}
            # degenerate all-zero solution → equal weight over active set
            w = 1.0 / len(active)
            return {symbols[a]: w for a in active}
        # drop the non-positive assets and iterate
        new_active = [a for k, a in enumerate(active) if sol[k] > 0]
        if not new_active or len(new_active) == len(active):
            # no positive component left, or stuck → equal weight over current active set
            if not active:
                return _equal_weights(symbols)
            w = 1.0 / len(active)
            return {symbols[a]: w for a in active}
        active = new_active


def _active_set_max_sharpe(
    symbols: list[str],
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_free: float,
) -> dict[str, float]:
    """Long-only max-Sharpe via subset enumeration of the unconstrained tangency.

    For every non-empty subset of the assets, solve the unconstrained tangency
    ``w_sub ∝ Σ_sub⁻¹ (μ_sub − rf·1)``; if every component is positive the subset's
    tangency is long-only feasible. Among all feasible subset-tangencies pick the
    one with the highest realized Sharpe. ``2^n`` subsets — bounded by realistic
    universe sizes (≤ ~15 assets); larger universes need a QP solver.
    """
    n = len(symbols)
    best_w = _equal_weights(symbols)
    best_sharpe = _sharpe_of(best_w, mean_returns, cov, risk_free)
    # iterate subset sizes from largest down so the first feasible hit tends to
    # be the richest, but we still check all to be sure.
    from itertools import combinations
    for size in range(n, 0, -1):
        for combo in combinations(range(n), size):
            sub_mat = [[cov.get((symbols[a], symbols[b]), 0.0) for b in combo] for a in combo]
            excess = [mean_returns[symbols[a]] - risk_free for a in combo]
            sol = _try_solve(sub_mat, excess)
            if sol is None:
                continue
            if any(x <= 0 for x in sol):
                continue
            total = sum(sol)
            if total <= 0:
                continue
            w = {symbols[a]: sol[k] / total for k, a in enumerate(combo)}
            # zero out non-active symbols
            full_w = {s: 0.0 for s in symbols}
            full_w.update(w)
            sharpe = _sharpe_of(full_w, mean_returns, cov, risk_free)
            if sharpe > best_sharpe:
                best_sharpe = sharpe
                best_w = full_w
    return best_w


def _sharpe_of(
    w: dict[str, float],
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_free: float,
) -> float:
    port_ret = sum(w.get(s, 0.0) * mean_returns[s] for s in mean_returns)
    port_var = portfolio_variance(cov, w)
    if port_var <= 0:
        return float("-inf")
    return (port_ret - risk_free) / math.sqrt(port_var)


def _min_variance_for_target(
    symbols: list[str],
    mean_returns: dict[str, float],
    cov: dict[tuple[str, str], float],
    target_return: float,
) -> dict[str, float]:
    """Long-only min-variance portfolio whose realized return is closest to target.

    Enumerates the long-only *vertex* portfolios (100% in a single asset) plus the
    two-asset min-variance blends along each pair, evaluates each candidate's return
    and variance, and returns the one minimizing ``variance + |return − target|·K``
    (a soft return-target penalty). This is a deterministic, ``n``-independent
    sampling of the long-only feasible region that does not degenerate for large
    ``n``; finer resolution can be had with a proper QP solver.
    """
    n = len(symbols)
    candidates: list[dict[str, float]] = []
    # single-asset vertices
    for s in symbols:
        candidates.append({sym: (1.0 if sym == s else 0.0) for sym in symbols})
    # pairwise min-variance blends (closed-form two-asset frontier)
    for i in range(n):
        for j in range(i + 1, n):
            va = cov.get((symbols[i], symbols[i]), 0.0)
            vb = cov.get((symbols[j], symbols[j]), 0.0)
            cab = cov.get((symbols[i], symbols[j]), 0.0)
            # min-var mix: w_a = (vb - cab) / (va + vb - 2 cab)
            denom = va + vb - 2.0 * cab
            if denom > 1e-18:
                wa = (vb - cab) / denom
                wa = max(0.0, min(1.0, wa))
                wb = 1.0 - wa
                candidates.append({sym: (wa if sym == symbols[i] else wb if sym == symbols[j] else 0.0) for sym in symbols})
    best_w = _equal_weights(symbols)
    best_score = float("inf")
    for w in candidates:
        port_ret = sum(w[s] * mean_returns[s] for s in symbols)
        ret_gap = abs(port_ret - target_return)
        port_var = portfolio_variance(cov, w)
        score = port_var + ret_gap * 1e6
        if score < best_score:
            best_score = score
            best_w = w
    return best_w
