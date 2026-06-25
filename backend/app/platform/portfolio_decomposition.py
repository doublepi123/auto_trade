"""P239: Portfolio Decomposition — returns to factors & contribution attribution.

Decompose a portfolio's realized return into **factor contributions** plus an
**idiosyncratic residual**, the way a Fama-French / Barra risk report does it:
OLS-regress the portfolio return series on a set of factor return series (with
intercept). The regression coefficients are the factor exposures (betas), the
intercept is the alpha, and per-factor average contribution is
``beta_i · mean(factor_i)``; the residual mean captures the idiosyncratic
(component) return.

* **returns_to_factors** — pure-Python OLS via the normal equations
  ``(XᵀX) b = Xᵀy`` solved by Gaussian elimination with partial pivoting;
  returns ``FactorExposureResult`` (alpha, per-factor beta, R², residual series,
  per-factor contribution, residual contribution). Raises ``ValueError`` on
  length mismatch / empty / singular design.
* **decompose_return** — reconcile ``sum(contributions) + residual`` against a
  stated total return and report the absolute reconciliation error.
* **variance_decomposition** — given asset weights ``w``, factor exposures
  ``B`` (assets × factors), factor covariance ``F`` (factors × factors) and a
  per-asset idiosyncratic variance, split portfolio variance into systematic
  (factor) and idiosyncratic parts and per-factor marginal contributions
  ``f_k = (Bᵀw)_k · (F·Bᵀw)_k`` (Menchero decomposition). Raises ``ValueError``
  on shape mismatch.

Deterministic, pure Python (standard library only — ``math`` / ``statistics``
/ ``itertools`` / ``collections`` / ``dataclasses`` / ``typing``). No numpy,
scipy, pandas, statsmodels, scikit-learn. Reference: Fama-French (1992/1993),
Barra risk model, Grinold & Kahn "Active Portfolio Management", Menchero
(2004) "Decomposing Risk". Pure Python, no scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Mapping, Sequence

__all__ = [
    "FactorExposureResult",
    "ReturnDecomposition",
    "VarianceDecomposition",
    "returns_to_factors",
    "decompose_return",
    "variance_decomposition",
]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _solve_linear_system(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve ``A x = b`` via Gaussian elimination with partial pivoting.

    Destructive on ``A``/``b``. Raises ``ValueError`` on a singular matrix
    (within a tight tolerance). Returns the solution vector.
    """
    n = len(A)
    if n == 0:
        raise ValueError("empty system")
    if any(len(row) != n for row in A):
        raise ValueError("A must be square")
    if len(b) != n:
        raise ValueError("b length must equal A dimension")
    # augmented matrix (work on a copy)
    M: list[list[float]] = [list(A[i]) + [b[i]] for i in range(n)]
    for col in range(n):
        # partial pivot
        pivot_row = col
        pivot_val = abs(M[col][col])
        for r in range(col + 1, n):
            v = abs(M[r][col])
            if v > pivot_val:
                pivot_val = v
                pivot_row = r
        if pivot_val < 1e-12:
            raise ValueError("singular design matrix; cannot solve OLS")
        if pivot_row != col:
            M[col], M[pivot_row] = M[pivot_row], M[col]
        pivot = M[col][col]
        # eliminate below
        for r in range(col + 1, n):
            factor = M[r][col] / pivot
            if factor == 0.0:
                continue
            for c in range(col, n + 1):
                M[r][c] -= factor * M[col][c]
    # back-substitution
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        s = M[i][n]
        for j in range(i + 1, n):
            s -= M[i][j] * x[j]
        x[i] = s / M[i][i]
    return x


# ---------------------------------------------------------------------------
# returns_to_factors
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactorExposureResult:
    alpha: float  # intercept (per-period alpha)
    betas: dict[str, float]  # factor name -> exposure (beta)
    r_squared: float  # centered R^2 of the regression
    residuals: list[float]  # OLS residual series (length = len(returns))
    factor_contribution: dict[str, float]  # beta_i * mean(factor_i)
    residual_contribution: float  # mean(residuals)
    n: int  # number of observations
    factors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "alpha": self.alpha,
            "betas": dict(self.betas),
            "r_squared": self.r_squared,
            "residuals": list(self.residuals),
            "factor_contribution": dict(self.factor_contribution),
            "residual_contribution": self.residual_contribution,
            "n": self.n,
            "factors": list(self.factors),
        }


def returns_to_factors(
    returns: Sequence[float],
    factor_returns: Mapping[str, Sequence[float]],
) -> FactorExposureResult:
    """OLS-regress portfolio returns on factor returns (with intercept).

    Given ``n`` periods with portfolio returns ``r`` (length ``n``) and a set
    of factor return series ``f_1, …, f_k`` (each length ``n``), solve

        r_t = α + Σ_i β_i · f_{i,t} + ε_t

    via the normal equations ``(XᵀX) b = Xᵀy`` (Gaussian elimination with
    partial pivoting). Coefficients ``b = (α, β_1, …, β_k)``. ``R² = 1 −
    SSR/SST`` (centered). Per-factor contribution ``c_i = β_i · mean(f_i)``,
    residual contribution ``= mean(ε)``.

    Raises ``ValueError`` on empty inputs, length mismatch between ``returns``
    and any factor series, fewer than ``k+1`` observations (under-determined),
    or a singular design (perfect collinearity / zero variance on all factors).
    """
    n = len(returns)
    if n == 0:
        raise ValueError("returns must be non-empty")
    if not factor_returns:
        raise ValueError("factor_returns must be non-empty")
    factors = list(factor_returns.keys())
    k = len(factors)
    # length check
    for f in factors:
        fr = factor_returns[f]
        if len(fr) != n:
            raise ValueError(
                f"factor '{f}' length {len(fr)} != returns length {n}"
            )
    if n < k + 1:
        raise ValueError(
            f"need at least k+1 = {k + 1} observations for {k} factors, got {n}"
        )
    # design matrix X (n × (k+1)): intercept column + factor columns
    # normal equations: XtX (k+1 × k+1), Xty (k+1)
    p = k + 1
    # build factor columns as lists
    fcols = [list(factor_returns[f]) for f in factors]
    XtX: list[list[float]] = [[0.0] * p for _ in range(p)]
    Xty: list[float] = [0.0] * p
    # row 0/col 0 = intercept
    # XtX[i][j] = sum over t of X[t][i] * X[t][j]; X[t][0] = 1, X[t][i>0] = fcols[i-1][t]
    for i in range(p):
        xi = [1.0] * n if i == 0 else fcols[i - 1]
        for j in range(i, p):
            xj = [1.0] * n if j == 0 else fcols[j - 1]
            s = 0.0
            for t in range(n):
                s += xi[t] * xj[t]
            XtX[i][j] = s
            XtX[j][i] = s
    # Xty[i] = sum X[t][i] * y[t]
    for i in range(p):
        xi = [1.0] * n if i == 0 else fcols[i - 1]
        s = 0.0
        for t in range(n):
            s += xi[t] * returns[t]
        Xty[i] = s
    coef = _solve_linear_system(XtX, Xty)
    alpha = coef[0]
    betas = {factors[i]: coef[i + 1] for i in range(k)}
    # residuals + R^2
    my = _mean(returns)
    ss_tot = sum((r - my) ** 2 for r in returns)
    residuals: list[float] = []
    ss_res = 0.0
    for t in range(n):
        pred = alpha + sum(betas[factors[i]] * fcols[i][t] for i in range(k))
        eps = returns[t] - pred
        residuals.append(eps)
        ss_res += eps * eps
    r2 = 1.0 - (ss_res / ss_tot) if ss_tot > 0 else 0.0
    # contributions
    factor_contrib = {f: betas[f] * _mean(factor_returns[f]) for f in factors}
    residual_contrib = _mean(residuals)
    return FactorExposureResult(
        alpha=alpha,
        betas=betas,
        r_squared=r2,
        residuals=residuals,
        factor_contribution=factor_contrib,
        residual_contribution=residual_contrib,
        n=n,
        factors=factors,
    )


# ---------------------------------------------------------------------------
# decompose_return
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReturnDecomposition:
    total_return: float
    contributions: dict[str, float]  # ordered as input
    residual: float
    reconciliation_error: float  # abs(total - (sum(contributions) + residual))

    def to_dict(self) -> dict:
        return {
            "total_return": self.total_return,
            "contributions": dict(self.contributions),
            "residual": self.residual,
            "reconciliation_error": self.reconciliation_error,
        }


def decompose_return(
    total_return: float,
    contributions: dict[str, float],
    residual: float,
) -> ReturnDecomposition:
    """Reconcile factor contributions + residual against a stated total return.

    Verifies ``Σ contributions + residual ≈ total_return`` and reports the
    absolute reconciliation error ``|total_return − (sum + residual)|``. The
    returned ``contributions`` preserve the input ordering. No exceptions are
    raised on mismatch — callers inspect ``reconciliation_error``.
    """
    if not isinstance(contributions, dict):
        raise ValueError("contributions must be a dict")
    # preserve insertion order
    ordered = dict(contributions)
    s = sum(ordered.values()) + residual
    err = abs(total_return - s)
    return ReturnDecomposition(
        total_return=float(total_return),
        contributions=ordered,
        residual=float(residual),
        reconciliation_error=err,
    )


# ---------------------------------------------------------------------------
# variance_decomposition
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class VarianceDecomposition:
    systematic_var: float  # wᵀ B F Bᵀ w
    idiosyncratic_var: float  # Σ w_i² · idio_var_i
    total_var: float  # systematic + idiosyncratic
    systematic_share: float  # systematic / total (0 if total==0)
    idiosyncratic_share: float  # idio / total (0 if total==0)
    per_factor_contribution: dict[str, float]  # factor_k -> (Bᵀw)_k · (F Bᵀw)_k
    per_factor_share: dict[str, float]  # factor_k -> contribution / total

    def to_dict(self) -> dict:
        return {
            "systematic_var": self.systematic_var,
            "idiosyncratic_var": self.idiosyncratic_var,
            "total_var": self.total_var,
            "systematic_share": self.systematic_share,
            "idiosyncratic_share": self.idiosyncratic_share,
            "per_factor_contribution": dict(self.per_factor_contribution),
            "per_factor_share": dict(self.per_factor_share),
        }


def variance_decomposition(
    weights: Sequence[float],
    factor_cov: list[list[float]],
    factor_exposures: list[float],
    idio_var: float,
) -> VarianceDecomposition:
    """Split portfolio variance into systematic (factor) + idiosyncratic parts.

    Single-factor / scalar-idio convenience form of the Barra factor model.
    Here ``factor_exposures`` is a per-asset vector of betas to *one* factor
    (length ``n``), ``factor_cov`` is the factor covariance matrix ``F``
    (k×k — must be square and consistent with the number of implied factors),
    ``weights`` is the asset weight vector ``w`` (length ``n``), and
    ``idio_var`` is a single idiosyncratic variance applied to every asset
    (i.e. ``D = idio_var · I``).

    For the multi-factor general case the per-factor marginal contribution is
    ``f_k = (Bᵀw)_k · (F·Bᵀw)_k`` and the systematic variance is
    ``Σ_k f_k = (Bᵀw)ᵀ F (Bᵀw) = wᵀ B F Bᵀ w``. With a single per-asset beta
    column ``B = [[b_i]]`` (n×1), ``Bᵀw`` is a 1-vector ``[Σ w_i b_i]`` and
    ``F`` reduces to a 1×1 ``[[σ_f²]]``; the systematic variance is therefore
    ``(Σ w_i b_i)² · σ_f²``. The idiosyncratic part is ``idio_var · Σ w_i²``.

    Raises ``ValueError`` on shape mismatch (weights/exposures length, or
    non-square ``factor_cov``).
    """
    n = len(weights)
    if n == 0:
        raise ValueError("weights must be non-empty")
    k = len(factor_cov)
    if k == 0:
        raise ValueError("factor_cov must be non-empty")
    for row in factor_cov:
        if len(row) != k:
            raise ValueError("factor_cov must be square (k×k)")
    m = len(factor_exposures)
    w = [float(x) for x in weights]
    # Two supported input shapes:
    #   (1) per-asset single-factor beta column (length n) with k==1:
    #       Bᵀw = [Σ w_i b_i]
    #   (2) already-aggregated factor exposure vector Bᵀw (length k):
    #       systematic = Bᵀw · F · Bᵀw
    if k == 1 and m == n:
        b = [float(x) for x in factor_exposures]
        BtW = [sum(w[i] * b[i] for i in range(n))]
    elif m == k:
        BtW = [float(x) for x in factor_exposures]
    else:
        raise ValueError(
            "factor_exposures must be a per-asset beta vector for a single "
            f"factor (length {n}) or match factor_cov dimension ({k}); got {m}"
        )
    F = [[float(x) for x in row] for row in factor_cov]
    # F · BtW
    FBtW = [sum(F[i][j] * BtW[j] for j in range(k)) for i in range(k)]
    # per-factor marginal contribution
    per_factor = [BtW[i] * FBtW[i] for i in range(k)]
    systematic = sum(per_factor)
    idio = idio_var * sum(wi * wi for wi in w)
    total = systematic + idio
    sys_share = systematic / total if total > 0 else 0.0
    idio_share = idio / total if total > 0 else 0.0
    per_factor_share = [
        (per_factor[i] / total) if total > 0 else 0.0 for i in range(k)
    ]
    # name factors by index for the single-column convenience form
    pf = {f"factor_{i}": per_factor[i] for i in range(k)}
    pfs = {f"factor_{i}": per_factor_share[i] for i in range(k)}
    return VarianceDecomposition(
        systematic_var=systematic,
        idiosyncratic_var=idio,
        total_var=total,
        systematic_share=sys_share,
        idiosyncratic_share=idio_share,
        per_factor_contribution=pf,
        per_factor_share=pfs,
    )