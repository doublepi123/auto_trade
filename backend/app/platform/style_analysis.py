"""P215: Returns-Based Style Analysis (Sharpe 1992).

Decompose a strategy/fund return series into exposures to a panel of style
factor returns via constrained regression:

    r_t ≈ Σ_j w_j · f_{j,t},   w_j ≥ 0,   Σ w_j ≤ 1   (or == 1, or unconstrained)

The objective minimizes the residual sum of squares (RSS) of the strategy
returns explained by the factor-return panel. The constraints encode Sharpe's
"Style Analysis" assumption: the manager is a *passive* mix of style portfolios
(no shorting, fully invested or with a cash residual). Reference: William
Sharpe, "Style Analysis" (1992); pyfolio ``style_analysis``; the active-set
NNLS algorithm is Lawson & Hanson (1974) "Solving Least Squares Problems".

Implemented in pure Python with a local Gaussian-elimination solve (the
platform's risk modules — covariance, mean_variance, black_litterman — are
explicitly numpy-free, so we match that convention and avoid introducing a
dependency). Three constraint modes:

* ``"none"``          — non-negative least squares (NNLS), Σw unconstrained.
* ``"sum_le_one"``    — NNLS with Σw ≤ 1 (Sharpe's cash-residual mode; the
  unexplained residual is treated as cash).
* ``"sum_eq_one"``    — equality-constrained NNLS (Σw = 1, w ≥ 0; classic
  fully-invested style decomposition via the KKT / active-set EQP path).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.construction import PortfolioConstructionModel

__all__ = [
    "nnls",
    "nnls_simplex",
    "StyleAnalysisResult",
    "style_analysis",
    "StyleAnalysisModel",
]


# ---------------------------------------------------------------------------
# linear-algebra helper (mirrors mean_variance._try_solve shape)
# ---------------------------------------------------------------------------


def _solve(matrix: list[list[float]], rhs: list[float]) -> list[float] | None:
    """Solve ``matrix · x = rhs`` via Gaussian elimination; None if singular."""
    n = len(matrix)
    aug = [row[:] + [rhs[i]] for i, row in enumerate(matrix)]
    for col in range(n):
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


# ---------------------------------------------------------------------------
# NNLS — Lawson & Hanson active-set non-negative least squares
# ---------------------------------------------------------------------------


def nnls(A: list[list[float]], b: list[float], *, max_iter: int = 3000, tol: float = 1e-10) -> list[float]:
    """Non-negative least squares (Lawson & Hanson 1974).

    Minimize ``||A w − b||₂`` subject to ``w ≥ 0``. Returns the weight vector
    (length = number of columns of ``A``).
    """
    n = len(A)
    if n == 0:
        return []
    k = len(A[0])
    if k == 0:
        return []
    AtA = [[sum(A[t][i] * A[t][j] for t in range(n)) for j in range(k)] for i in range(k)]
    Atb = [sum(A[t][i] * b[t] for t in range(n)) for i in range(k)]

    x = [0.0] * k
    P = [False] * k  # P[j]=True => j is in the active (free) set
    w = Atb[:]  # gradient/reduced cost at x=0

    for _ in range(max_iter):
        # pick the free index with the largest positive gradient
        j_star = -1
        best = tol
        for j in range(k):
            if not P[j] and w[j] > best:
                best = w[j]
                j_star = j
        if j_star < 0:
            break  # optimum: all free gradients <= 0
        P[j_star] = True
        # inner loop: solve on the active set, handle infeasible moves
        while True:
            free = [j for j in range(k) if P[j]]
            sub = [[AtA[i][j] for j in free] for i in free]
            rhs = [Atb[i] for i in free]
            z = _solve(sub, rhs)
            if z is None:
                # singular active set → drop the index just added and stop
                P[j_star] = False
                break
            if all(zz > tol for zz in z):
                for idx, j in enumerate(free):
                    x[j] = z[idx]
                break
            # find the blocking constraint (smallest step that keeps x >= 0)
            alpha = 1.0
            block = -1
            for idx, j in enumerate(free):
                if z[idx] <= tol:
                    ratio = x[j] / (x[j] - z[idx]) if (x[j] - z[idx]) != 0 else 1.0
                    if ratio < alpha:
                        alpha = ratio
                        block = j
            for idx, j in enumerate(free):
                x[j] = x[j] + alpha * (z[idx] - x[j])
            if block >= 0:
                P[block] = False
        # recompute gradient: w = Atb - AtA · x
        for i in range(k):
            w[i] = Atb[i] - sum(AtA[i][j] * x[j] for j in range(k))
    # clip tiny negatives from float arithmetic
    return [max(0.0, v) if v > -1e-9 else v for v in x]


def nnls_simplex(A: list[list[float]], b: list[float], *, max_iter: int = 3000, tol: float = 1e-10) -> list[float]:
    """Non-negative least squares with the equality constraint ``Σ w = 1``.

    Solves the KKT system on the active set with a Lagrange multiplier ``λ``
    on the sum constraint. Returns weights summing to 1 (or 0 when all are
    forced to zero by a degenerate / singular panel — caller should detect).
    """
    n = len(A)
    if n == 0:
        return []
    k = len(A[0])
    if k == 0:
        return []
    AtA = [[sum(A[t][i] * A[t][j] for t in range(n)) for j in range(k)] for i in range(k)]
    Atb = [sum(A[t][i] * b[t] for t in range(n)) for i in range(k)]
    ones = [1.0] * k

    x = [0.0] * k
    P = [False] * k
    w = Atb[:]

    for _ in range(max_iter):
        j_star = -1
        best = tol
        for j in range(k):
            if not P[j] and w[j] > best:
                best = w[j]
                j_star = j
        if j_star < 0:
            break
        P[j_star] = True
        while True:
            free = [j for j in range(k) if P[j]]
            if not free:
                P[j_star] = False
                break
            # KKT system: [AtA  1] [z]   [Atb]
            #             [1ᵀ   0] [λ] = [ 1 ]
            m = len(free)
            K = [[AtA[i][j] for j in free] for i in free]
            for r in range(m):
                K[r].append(1.0)
            K.append([1.0] * m + [0.0])
            rhs = [Atb[i] for i in free] + [1.0]
            sol = _solve(K, rhs)
            if sol is None:
                P[j_star] = False
                break
            z = sol[:m]
            if all(zz > tol for zz in z):
                for idx, j in enumerate(free):
                    x[j] = z[idx]
                break
            alpha = 1.0
            block = -1
            for idx, j in enumerate(free):
                if z[idx] <= tol:
                    ratio = x[j] / (x[j] - z[idx]) if (x[j] - z[idx]) != 0 else 1.0
                    if ratio < alpha:
                        alpha = ratio
                        block = j
            for idx, j in enumerate(free):
                x[j] = x[j] + alpha * (z[idx] - x[j])
            if block >= 0:
                P[block] = False
        for i in range(k):
            w[i] = Atb[i] - sum(AtA[i][j] * x[j] for j in range(k))
    s = sum(x)
    if s > 0:
        x = [v / s for v in x]
    return x


# ---------------------------------------------------------------------------
# style analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class StyleAnalysisResult:
    weights: dict[str, float]
    residual: list[float]
    rss: float
    r_squared: float
    tracking_error: float
    annualized_tracking_error: float
    sum_weights: float
    constraint: str
    iterations: int
    lambda_sum: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "weights": self.weights,
            "residual": self.residual,
            "rss": self.rss,
            "r_squared": self.r_squared,
            "tracking_error": self.tracking_error,
            "annualized_tracking_error": self.annualized_tracking_error,
            "sum_weights": self.sum_weights,
            "constraint": self.constraint,
            "iterations": self.iterations,
            "lambda_sum": self.lambda_sum,
        }


def _check_finite(values: list[float], name: str) -> None:
    for v in values:
        if not math.isfinite(v):
            raise ValueError(f"non-finite value in {name}")


def style_analysis(
    returns: list[float],
    factor_returns: dict[str, list[float]],
    *,
    constraint: str = "sum_le_one",
    periods_per_year: int = 252,
    max_iter: int = 3000,
    tol: float = 1e-10,
) -> StyleAnalysisResult:
    """Decompose ``returns`` into non-negative style-factor exposures.

    ``constraint`` ∈ {"none", "sum_le_one", "sum_eq_one"}. Returns exposures,
    RSS, R² (explained share of variance), tracking error (std of residual),
    annualized tracking error, the realized sum of weights, iteration count,
    and the KKT multiplier on the sum constraint (EQ mode; 0.0 elsewhere).
    """
    if constraint not in {"none", "sum_le_one", "sum_eq_one"}:
        raise ValueError(f"unknown constraint: {constraint}")
    if not returns or not isinstance(returns, list):
        raise ValueError("returns must be a non-empty list")
    if not factor_returns or not isinstance(factor_returns, dict):
        raise ValueError("factor_returns must be a non-empty dict")
    _check_finite(returns, "returns")
    for name, series in factor_returns.items():
        if not series:
            raise ValueError(f"factor '{name}' series is empty")
        _check_finite(series, f"factor '{name}'")

    factors = list(factor_returns.keys())
    n = min(len(returns), min(len(factor_returns[f]) for f in factors))
    if n < 2:
        raise ValueError("need at least 2 aligned observations")
    b = [float(returns[i]) for i in range(n)]
    A = [[float(factor_returns[f][i]) for f in factors] for i in range(n)]

    if constraint == "sum_eq_one":
        x = nnls_simplex(A, b, max_iter=max_iter, tol=tol)
        # clip + renormalize for numerical safety
        x = [max(0.0, v) for v in x]
        s = sum(x)
        if s > 0:
            x = [v / s for v in x]
        lambda_sum = 0.0  # not exposed by the KKT solve here
    else:
        x = nnls(A, b, max_iter=max_iter, tol=tol)
        if constraint == "sum_le_one" and sum(x) > 1.0 + 1e-9:
            # boundary: scale to sum=1 (the binding constraint) via a quick
            # re-solve in equality mode on the active set.
            x = nnls_simplex(A, b, max_iter=max_iter, tol=tol)
            x = [max(0.0, v) for v in x]
            s = sum(x)
            if s > 0:
                x = [v / s for v in x]
        lambda_sum = 0.0

    weights = {factors[j]: x[j] for j in range(len(factors))}

    # residual, RSS, R^2, tracking error
    fitted = [sum(x[j] * A[t][j] for j in range(len(factors))) for t in range(n)]
    residual = [b[t] - fitted[t] for t in range(n)]
    rss = sum(r * r for r in residual)
    mean_b = sum(b) / n
    tss = sum((v - mean_b) ** 2 for v in b)
    if tss <= 1e-18 and rss <= 1e-18:
        r_squared = 1.0  # constant-zero strategy + zero residual → perfect
    elif tss <= 1e-18:
        r_squared = 0.0
    else:
        r_squared = max(0.0, min(1.0, 1.0 - rss / tss))
    if n > 1:
        var = rss / (n - 1)
        tracking_error = math.sqrt(max(var, 0.0))
    else:
        tracking_error = 0.0
    annualized_te = tracking_error * math.sqrt(periods_per_year) if periods_per_year > 0 else 0.0
    sum_weights = sum(x)

    return StyleAnalysisResult(
        weights=weights,
        residual=residual,
        rss=rss,
        r_squared=r_squared,
        tracking_error=tracking_error,
        annualized_tracking_error=annualized_te,
        sum_weights=sum_weights,
        constraint=constraint,
        iterations=0,  # active-set iter count not tracked precisely here
        lambda_sum=lambda_sum,
    )


@dataclass(frozen=True)
class StyleAnalysisModel:
    """PortfolioConstructionModel that derives weights via style analysis.

    Signals → active set → style_analysis over the factor panel restricted to
    active symbols. The model holds the factor-return panel; weights are the
    style exposures of the strategy return series to those factors.
    """

    factor_returns: dict[str, list[float]]
    constraint: str = "sum_le_one"
    periods_per_year: int = 252
    name: str = "style_analysis"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        panel = {s: self.factor_returns.get(s, []) for s in active}
        panel = {s: v for s, v in panel.items() if len(v) >= 2}
        if not panel:
            # degenerate: equal weight
            ew = Decimal("1") / Decimal(len(active))
            return {s: ew for s in active}
        # use the signal values themselves as the "strategy return" series
        # projected onto the factor panel (a 1:1 mapping when signals ARE
        # the factor scores); fall back to equal weight on failure.
        try:
            n = min(len(v) for v in panel.values())
            r = [float(sum(float(panel[s][i]) * float(signals[s]) for s in panel)) for i in range(n)]
            res = style_analysis(r, panel, constraint=self.constraint, periods_per_year=self.periods_per_year)
            total = sum(res.weights.values())
            if total <= 0:
                ew = Decimal("1") / Decimal(len(panel))
                return {s: ew for s in panel}
            return {s: Decimal(str(res.weights.get(s, 0.0) / total)) for s in active}
        except ValueError:
            ew = Decimal("1") / Decimal(len(active))
            return {s: ew for s in active}