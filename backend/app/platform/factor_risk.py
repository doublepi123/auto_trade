"""P230: Factor Risk Decomposition (Cross-Sectional).

Decompose a portfolio's ex-ante risk into contributions from a set of
statistical or fundamental factors, vs idiosyncratic residual risk. Given a
factor covariance matrix ``F`` (k×k), a factor exposure matrix ``B`` (n×k,
one row per asset's beta to each factor), a diagonal idiosyncratic variance
``D`` (n×n), and weight vector ``w`` (n), the portfolio variance is

    σ²_p = wᵀ (B F Bᵀ + D) w = wᵀ B F Bᵀ w + wᵀ D w

The **factor contribution** is ``wᵀ B F Bᵀ w`` (decomposable per factor via
``(Bᵀ w)ᵀ F (Bᵀ w)`` when F is diagonal, or per-factor marginal contribution
``f_k = (Bᵀ w)_k · (F Bᵀ w)_k``), and the **idiosyncratic contribution** is
``wᵀ D w``. We return both plus the per-factor share.

This is the Barra / Axioma style risk model used in sell-side risk reports,
made explicit so callers can attribute variance to style/beta/sector factors.

Reference: Barra risk model, Grinold & Kahn "Active Portfolio Management",
Menchero (2004) "Decomposing Risk".

Deterministic, pure Python.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Mapping, Sequence

__all__ = [
    "FactorRiskDecomposition",
    "factor_risk_decomposition",
    "matrix_mult_vec",
    "gram_matrix",
]


def matrix_mult_vec(M: Sequence[Sequence[float]], v: Sequence[float]) -> list[float]:
    """Matrix (list of rows) × column vector."""
    if not M:
        return []
    n_cols = len(M[0])
    if n_cols != len(v):
        raise ValueError("matrix columns must equal vector length")
    return [sum(M[i][j] * v[j] for j in range(n_cols)) for i in range(len(M))]


def gram_matrix(B: Sequence[Sequence[float]], F: Sequence[Sequence[float]]) -> list[list[float]]:
    """Compute ``B F Bᵀ`` (n×n). ``B`` is n×k, ``F`` is k×k."""
    n = len(B)
    if n == 0:
        return []
    k = len(B[0])
    if any(len(row) != k for row in B):
        raise ValueError("B rows must all have k columns")
    if len(F) != k or any(len(row) != k for row in F):
        raise ValueError("F must be k×k")
    # B F  → n×k
    BF: list[list[float]] = [[0.0] * k for _ in range(n)]
    for i in range(n):
        for j in range(k):
            BF[i][j] = sum(B[i][m] * F[m][j] for m in range(k))
    # (BF) B^T  → n×n
    out: list[list[float]] = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            out[i][j] = sum(BF[i][m] * B[j][m] for m in range(k))
    return out


@dataclass(frozen=True)
class FactorRiskDecomposition:
    portfolio_variance: float
    portfolio_volatility: float
    factor_variance: float
    idiosyncratic_variance: float
    factor_share: float  # factor_variance / portfolio_variance
    per_factor_variance: dict[str, float]  # factor name -> variance contribution
    per_factor_share: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "portfolio_variance": self.portfolio_variance,
            "portfolio_volatility": self.portfolio_volatility,
            "factor_variance": self.factor_variance,
            "idiosyncratic_variance": self.idiosyncratic_variance,
            "factor_share": self.factor_share,
            "per_factor_variance": self.per_factor_variance,
            "per_factor_share": self.per_factor_share,
        }


def factor_risk_decomposition(
    weights: Mapping[str, float],
    exposures: Mapping[str, dict[str, float]],
    factor_cov: Mapping[str, Mapping[str, float]],
    idio_var: Mapping[str, float],
) -> FactorRiskDecomposition:
    """Decompose portfolio variance into factor + idiosyncratic contributions.

    - ``weights``: ``{symbol: w}``
    - ``exposures``: ``{symbol: {factor: beta}}``  (B matrix, n×k)
    - ``factor_cov``: ``{factor_i: {factor_j: cov}}`` (F matrix, k×k)
    - ``idio_var``: ``{symbol: σ²_idio}`` (diagonal D)

    Per-factor variance contribution uses
    ``f_k = (Bᵀw)_k · (F·(Bᵀw))_k`` (Menchero marginal decomposition), which
    sums to the total factor variance when F is symmetric.
    """
    if not weights:
        raise ValueError("weights must be non-empty")
    symbols = list(weights.keys())
    n = len(symbols)
    factors = list(factor_cov.keys())
    k = len(factors)
    if k == 0:
        raise ValueError("factor_cov must be non-empty")
    w = [float(weights[s]) for s in symbols]
    # B matrix n×k
    B: list[list[float]] = []
    for s in symbols:
        row = [float(exposures.get(s, {}).get(f, 0.0)) for f in factors]
        B.append(row)
    # B^T w  → k-vector (factor portfolio exposures)
    BtW = [sum(B[i][j] * w[i] for i in range(n)) for j in range(k)]
    # F (k×k) as list of rows
    F = [[float(factor_cov[fi].get(fj, 0.0)) for fj in factors] for fi in factors]
    # F · BtW
    FBtW = matrix_mult_vec(F, BtW)
    # per-factor variance: f_k = BtW_k * FBtW_k (marginal)
    per_factor_var = {factors[j]: BtW[j] * FBtW[j] for j in range(k)}
    factor_var = sum(per_factor_var.values())
    # idiosyncratic: sum w_i^2 * idio_var_i
    idio = sum((w[i] ** 2) * float(idio_var.get(symbols[i], 0.0)) for i in range(n))
    port_var = factor_var + idio
    vol = math.sqrt(max(port_var, 0.0))
    per_factor_share = {f: (per_factor_var[f] / port_var if port_var > 0 else 0.0) for f in factors}
    return FactorRiskDecomposition(
        portfolio_variance=port_var,
        portfolio_volatility=vol,
        factor_variance=factor_var,
        idiosyncratic_variance=idio,
        factor_share=factor_var / port_var if port_var > 0 else 0.0,
        per_factor_variance=per_factor_var,
        per_factor_share=per_factor_share,
    )