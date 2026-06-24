"""P205: Black-Litterman portfolio construction.

The Black-Litterman (1991) model blends a market-implied prior of expected
returns with an investor's subjective views, producing a posterior expected-
return vector and (optionally) a posterior covariance. The result feeds a
mean-variance optimizer (P204) to get portfolio weights that respect both the
market equilibrium and the investor's views.

Reference: Black & Litterman (1991); PyPortfolioOpt's ``BlackLittermanModel``.
Pure Python, dict-based I/O, deterministic.

A view is ``(assets: dict[str,float], expected_return: float, confidence:
float)`` where ``assets`` holds the view's per-asset weights (e.g. {A: 1} for an
absolute view on A, {A: 1, B: -1} for a relative A-vs-B view) and
``confidence`` ∈ (0, 1] scales the view's uncertainty row in Ω.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.construction import PortfolioConstructionModel
from app.platform.mean_variance import max_sharpe_weights

__all__ = [
    "View",
    "market_implied_returns",
    "black_litterman",
    "BlackLittermanModel",
]


@dataclass(frozen=True)
class View:
    """An investor view: the weighted combination of ``assets`` earns ``expected_return``.

    ``confidence`` ∈ (0, 1] scales the view's uncertainty row in Ω via Idzorek-style
    scaling: ``Ω_r = ((1−c)/c) · (P τΣ Pᵀ)_rr``. ``c=1`` ⇒ the view binds exactly
    (Ω→0); ``c→0`` ⇒ the view is ignored (Ω→∞). Values outside (0, 1] are clamped.
    """

    assets: dict[str, float]
    expected_return: float
    confidence: float = 1.0  # higher = more certain view


def market_implied_returns(
    market_weights: dict[str, float],
    cov: dict[tuple[str, str], float],
    risk_aversion: float = 2.5,
) -> dict[str, float]:
    """Reverse-optimization prior: π = δ · Σ · w_mkt.

    The equilibrium excess expected returns implied by the market portfolio
    weights under a mean-variance assumption with risk-aversion ``δ``.
    """
    symbols = sorted(market_weights.keys())
    w = [market_weights[s] for s in symbols]
    pi: dict[str, float] = {}
    for i, s_i in enumerate(symbols):
        acc = 0.0
        for j, s_j in enumerate(symbols):
            acc += risk_aversion * cov.get((s_i, s_j), 0.0) * w[j]
        pi[s_i] = acc
    return pi


def black_litterman(
    prior: dict[str, float],
    cov: dict[tuple[str, str], float],
    views: list[View],
    tau: float = 0.05,
) -> tuple[dict[str, float], dict[tuple[str, str], float]]:
    """Posterior expected returns and covariance under Black-Litterman.

    Returns ``(posterior_returns, posterior_cov)``:
        E[R] = π + τΣ Pᵀ (P τΣ Pᵀ + Ω)⁻¹ (Q − Pπ)
        Σ_BL = Σ + τΣ − τΣ Pᵀ (P τΣ Pᵀ + Ω)⁻¹ P τΣ

    where P is the view-picking matrix, Q the view returns, and Ω the view
    uncertainty diagonal. With no views the posterior equals the prior and the
    covariance is unchanged (scaled by 1+τ).
    """
    symbols = sorted(prior.keys())
    n = len(symbols)
    if n == 0 or not views:
        # No views: posterior = prior; cov scaled by (1 + tau) to reflect the
        # extra estimation uncertainty BL attributes to the prior.
        scaled = {(s_i, s_j): (1.0 + tau) * cov.get((s_i, s_j), 0.0)
                  for s_i in symbols for s_j in symbols}
        return dict(prior), scaled

    idx = {s: i for i, s in enumerate(symbols)}
    k = len(views)
    # P (k x n), Q (k)
    P = [[0.0] * n for _ in range(k)]
    Q = [0.0] * k
    for r, view in enumerate(views):
        for asset, weight in view.assets.items():
            if asset in idx:
                P[r][idx[asset]] = weight
        Q[r] = view.expected_return

    tau_sigma = {(s_i, s_j): tau * cov.get((s_i, s_j), 0.0)
                 for s_i in symbols for s_j in symbols}

    # P @ tau_sigma @ P^T  (k x k) — the per-view uncertainty scale.
    PtSP = _matmul_mat_diag(P, tau_sigma, symbols)

    # Ω (view-uncertainty diagonal). Scaled to the same units as ``P τΣ Pᵀ`` so
    # that ``confidence`` has a real, dimensionally-consistent effect: a view
    # with confidence ``c`` gets Ω_r = ((1−c)/c) · (P τΣ Pᵀ)_rr (Idzorek-style
    # confidence scaling). c→1 ⇒ Ω→0 (view binds exactly); c→0 ⇒ Ω→∞ (view
    # ignored). Floor at a tiny ε so the matrix stays invertible.
    omega_diag = [0.0] * k
    for r, view in enumerate(views):
        c = max(1e-6, min(1.0, view.confidence))
        base = PtSP[r][r] if PtSP[r][r] > 0 else 1e-12
        omega_diag[r] = max(1e-18, (1.0 - c) / c * base)
    for r in range(k):
        PtSP[r][r] += omega_diag[r]

    inv_ptsp = _invert(PtSP)
    if inv_ptsp is None:
        # Singular → fall back to prior
        scaled = {(s_i, s_j): (1.0 + tau) * cov.get((s_i, s_j), 0.0)
                  for s_i in symbols for s_j in symbols}
        return dict(prior), scaled

    # residual Q - Pπ
    ppi = [sum(P[r][c] * prior[symbols[c]] for c in range(n)) for r in range(k)]
    residual = [Q[r] - ppi[r] for r in range(k)]

    # g = inv_ptsp @ residual  (k-vector)
    g = [sum(inv_ptsp[r][s] * residual[s] for s in range(k)) for r in range(k)]

    # posterior mean = π + τΣ · P^T · g
    posterior = dict(prior)
    for c, s_c in enumerate(symbols):
        correction = 0.0
        for r in range(k):
            ts_dot_pr = sum(tau_sigma[(symbols[cc], s_c)] * P[r][cc] for cc in range(n))
            correction += ts_dot_pr * g[r]
        posterior[s_c] = prior[s_c] + correction

    # posterior cov = Sigma + tau_sigma - tau_sigma P^T inv_ptsp P tau_sigma
    post_cov: dict[tuple[str, str], float] = {}
    for ci, s_i in enumerate(symbols):
        for cj, s_j in enumerate(symbols):
            base = cov.get((s_i, s_j), 0.0) + tau_sigma[(s_i, s_j)]
            subtract = 0.0
            for r in range(k):
                ts_i = sum(tau_sigma[(symbols[cc], s_i)] * P[r][cc] for cc in range(n))
                for s in range(k):
                    ts_j = sum(tau_sigma[(symbols[cc], s_j)] * P[s][cc] for cc in range(n))
                    subtract += ts_i * inv_ptsp[r][s] * ts_j
            post_cov[(s_i, s_j)] = base - subtract
    return posterior, post_cov


# ---- construction model -----------------------------------------------------


@dataclass(frozen=True)
class BlackLittermanModel:
    """PortfolioConstructionModel running views through Black-Litterman → max-Sharpe."""

    market_weights: dict[str, float]
    cov: dict[tuple[str, str], float]
    views: list[View]
    tau: float = 0.05
    risk_aversion: float = 2.5
    risk_free: float = 0.0
    name: str = "black_litterman"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        prior = market_implied_returns(self.market_weights, self.cov, self.risk_aversion)
        posterior, _ = black_litterman(prior, self.cov, self.views, tau=self.tau)
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        mu = {s: posterior.get(s, 0.0) for s in active}
        cov_active = {(a, b): self.cov.get((a, b), 0.0) for a in active for b in active}
        w = max_sharpe_weights(mu, cov_active, risk_free=self.risk_free)
        return {s: Decimal(str(w.get(s, 0.0))) for s in active}


# ---- linear-algebra helpers (pure Python, no numpy) -------------------------


def _matmul_mat_diag(
    P: list[list[float]], sigma: dict[tuple[str, str], float], symbols: list[str]
) -> list[list[float]]:
    """P @ Sigma @ P^T where Sigma is pair-keyed."""
    k = len(P)
    n = len(symbols)
    # Sigma matrix dense
    S = [[sigma[(symbols[i], symbols[j])] for j in range(n)] for i in range(n)]
    # PS = P @ S  (k x n)
    PS = [[sum(P[r][c] * S[c][j] for c in range(n)) for j in range(n)] for r in range(k)]
    # PtSP = PS @ P^T  (k x k)
    return [[sum(PS[r][c] * P[s][c] for c in range(n)) for s in range(k)] for r in range(k)]


def _invert(matrix: list[list[float]]) -> list[list[float]] | None:
    """Invert a small dense matrix via Gauss-Jordan; None if singular."""
    n = len(matrix)
    aug = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(aug[r][col]))
        if abs(aug[pivot][col]) < 1e-12:
            return None
        aug[col], aug[pivot] = aug[pivot], aug[col]
        pivot_val = aug[col][col]
        for c in range(2 * n):
            aug[col][c] /= pivot_val
        for r in range(n):
            if r == col:
                continue
            factor = aug[r][col]
            for c in range(2 * n):
                aug[r][c] -= factor * aug[col][c]
    return [[aug[i][n + j] for j in range(n)] for i in range(n)]
