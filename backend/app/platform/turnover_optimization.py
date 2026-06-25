"""P216: Turnover-Aware / Transaction-Cost Portfolio Optimization.

Markowitz with a penalty for moving away from the *current* book. The
greenfield mean-variance optimizer (P204) ignores the cost of rebalancing, so
its targets churn excessively — a min-variance book that flips 40% per
rebalance is dominated by transaction costs. This module solves the long-only
problem with turnover in the objective (and an optional hard turnover cap):

    min  wᵀΣw − λ (μ − rf)ᵀ w + γ · Σ_i |w_i − w_prev,i|
    s.t. Σw = 1, w ≥ 0, (optional) Σ_i |w_i − w_prev,i| ≤ delta_cap

The L1 turnover term is non-smooth, so we use projected subgradient descent:
the simplex projection (Duchi 2008) keeps every iterate feasible (Σw=1, w≥0),
and the L1 subgradient ``γ·sign(w − w_prev)`` handles the turnover term. An
optional hard turnover cap is enforced by a proximal projection back toward
the previous weights.

Reference: PyPortfolioOpt ``EfficientFronter`` with turnover
constraint/objective; Mitchell & Braun, "Mean-Variance Portfolio Optimization
with Transaction Costs". Pure Python (mirrors the platform's
:mod:`app.platform.mean_variance` and :mod:`app.platform.covariance` style —
no new third-party dependency).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.construction import PortfolioConstructionModel
from app.platform.covariance import matrix_from_pairs, portfolio_variance
from app.platform.mean_variance import max_sharpe_weights, min_variance_weights

__all__ = [
    "turnover_aware_optimize",
    "TurnoverAwareModel",
    "turnover_penalty",
    "turnover_objective",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _sanitize_prev(prev: dict[str, float]) -> dict[str, float]:
    """Drop negatives and renormalize ``prev`` to sum 1; empty if all-zero/empty."""
    if not prev:
        return {}
    cleaned = {s: max(0.0, v) for s, v in prev.items()}
    total = sum(cleaned.values())
    if total <= 0:
        return {}
    return {s: v / total for s, v in cleaned.items()}


def _project_simplex(v: list[float]) -> list[float]:
    """Euclidean projection onto {w ≥ 0, Σw = 1} (Duchi 2008)."""
    n = len(v)
    if n == 0:
        return []
    u = sorted(v, reverse=True)
    cssv = 0.0
    rho = 0
    theta = 0.0
    for i in range(n):
        cssv += u[i]
        t = (cssv - 1.0) / (i + 1)
        if u[i] - t > 0:
            rho = i + 1
            theta = t
    return [max(0.0, vi - theta) for vi in v]


def turnover_penalty(weights: dict[str, float], prev_weights: dict[str, float]) -> float:
    """L1 turnover ``Σ_i |w_i − w_prev,i|`` over the union of symbols."""
    symbols = set(weights) | set(prev_weights)
    return sum(abs(weights.get(s, 0.0) - prev_weights.get(s, 0.0)) for s in symbols)


def turnover_objective(
    weights: dict[str, float],
    cov: dict[tuple[str, str], float],
    mu: dict[str, float],
    prev_weights: dict[str, float],
    gamma: float,
    lam: float,
    risk_free: float = 0.0,
) -> float:
    """f(w) = wᵀΣw − λ(μ−rf)ᵀw + γ·turnover(w, w_prev)."""
    pv = portfolio_variance(cov, weights)
    ret = sum(weights.get(s, 0.0) * (mu.get(s, 0.0) - risk_free) for s in mu)
    return pv - lam * ret + gamma * turnover_penalty(weights, prev_weights)


# ---------------------------------------------------------------------------
# optimizer
# ---------------------------------------------------------------------------


def turnover_aware_optimize(
    prev_weights: dict[str, float],
    cov: dict[tuple[str, str], float],
    mu: dict[str, float] | None = None,
    gamma: float = 1.0,
    delta_cap: float | None = None,
    risk_free: float = 0.0,
    lam: float = 1.0,
    max_iter: int = 200,
    tol: float = 1e-7,
) -> dict[str, float]:
    """Long-only turnover-aware Markowitz optimizer.

    Minimizes ``wᵀΣw − λ(μ−rf)ᵀw + γ·Σ|w_i − w_prev,i|`` over the simplex
    {w≥0, Σw=1}, with an optional hard turnover cap ``|w − w_prev|₁ ≤ delta_cap``.
    Returns ``{symbol: weight}`` summing to 1.0. Deterministic (no RNG).
    """
    symbols = sorted({s for pair in cov for s in pair})
    if not symbols:
        return {}
    n = len(symbols)
    if n == 1:
        return {symbols[0]: 1.0}

    # NaN guard
    for (i, j), v in cov.items():
        if not math.isfinite(v):
            raise ValueError("NaN/inf in cov")
    if mu:
        for s, v in mu.items():
            if not math.isfinite(v):
                raise ValueError("NaN/inf in mu")

    sigma = matrix_from_pairs(cov, symbols)
    mu_vec = [mu.get(s, 0.0) for s in symbols] if mu else [0.0] * n
    prev = _sanitize_prev(prev_weights)
    prev_vec = [prev.get(s, 0.0) for s in symbols]

    # Degenerate: no previous book + no return view → plain min-variance.
    if not prev and (not mu or all(v == 0.0 for v in mu_vec)):
        return min_variance_weights(cov=cov)
    if not prev:
        # greenfield: turnover term is zero everywhere → standard MV optimum.
        if lam > 0 and mu:
            return max_sharpe_weights(mu, cov, risk_free=risk_free)
        return min_variance_weights(cov=cov)

    # initialize at the projected previous weights
    w = _project_simplex(list(prev_vec))
    if sum(w) <= 0:
        w = [1.0 / n] * n

    def turnover_of(wv: list[float]) -> float:
        return sum(abs(wv[i] - prev_vec[i]) for i in range(n))

    best_w = list(w)
    best_obj = turnover_objective(
        {symbols[i]: w[i] for i in range(n)}, cov, {symbols[i]: mu_vec[i] for i in range(n)},
        prev, gamma, lam, risk_free,
    )

    alpha0 = 1.0
    beta = 0.05
    stable = 0
    for k in range(max_iter):
        # gradient of the smooth part: 2 Σw − λ(μ − rf)
        grad = [0.0] * n
        for i in range(n):
            s_i = 0.0
            for j in range(n):
                s_i += sigma[i][j] * w[j]
            grad[i] = 2.0 * s_i - lam * (mu_vec[i] - risk_free)
        # L1 subgradient of the turnover term: γ·sign(w − w_prev)
        sub = [gamma * (1.0 if w[i] > prev_vec[i] else -1.0 if w[i] < prev_vec[i] else 0.0)
               for i in range(n)]
        step = alpha0 / (1.0 + beta * k)
        w_new = [w[i] - step * (grad[i] + sub[i]) for i in range(n)]
        w_new = _project_simplex(w_new)
        # optional hard turnover cap: scale displacement toward prev.
        if delta_cap is not None:
            t = turnover_of(w_new)
            if t > delta_cap and t > 0:
                scale = delta_cap / t
                w_new = [prev_vec[i] + scale * (w_new[i] - prev_vec[i]) for i in range(n)]
                w_new = _project_simplex(w_new)
        # convergence check
        delta = max(abs(w_new[i] - w[i]) for i in range(n))
        w = w_new
        # track best feasible iterate by objective
        w_dict = {symbols[i]: w[i] for i in range(n)}
        obj = turnover_objective(w_dict, cov, {symbols[i]: mu_vec[i] for i in range(n)},
                                  prev, gamma, lam, risk_free)
        if obj < best_obj - 1e-15:
            best_obj = obj
            best_w = list(w)
        if delta < tol:
            stable += 1
            if stable >= 5:
                break
        else:
            stable = 0

    # final cleanup: clamp tiny negatives, renormalize
    result = {symbols[i]: max(0.0, best_w[i]) for i in range(n)}
    total = sum(result.values())
    if total > 0:
        result = {s: v / total for s, v in result.items()}
    return result


@dataclass(frozen=True)
class TurnoverAwareModel:
    """PortfolioConstructionModel that sizes via turnover-aware optimization.

    Builds a diagonal covariance from ``volatilities`` and μ from ``signals``,
    optimizes against its ``prev_weights`` (zero-filled for new symbols).
    """

    prev_weights: dict[str, float] | None = None
    gamma: float = 1.0
    delta_cap: float | None = None
    risk_free: float = 0.0
    lam: float = 1.0
    name: str = "turnover_aware"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        vols = volatilities or {}
        if all(float(vols[s]) > 0 for s in active) and len(vols) >= len(active):
            cov = {(a, b): (float(vols[a]) ** 2 if a == b else 0.0) for a in active for b in active}
        else:
            cov = {(a, b): (0.04 if a == b else 0.0) for a in active for b in active}
        mu = {s: float(signals[s]) for s in active}
        prev = self.prev_weights or {}
        w = turnover_aware_optimize(
            {s: prev.get(s, 0.0) for s in active}, cov, mu,
            gamma=self.gamma, delta_cap=self.delta_cap,
            risk_free=self.risk_free, lam=self.lam,
        )
        return {s: Decimal(str(w.get(s, 0.0))) for s in active}