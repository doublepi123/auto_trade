"""P383: Quadratic Factor Model — linear + squared + interaction terms.

Builds a design matrix with constant, linear (first-order) factors,
squared terms (f_i²), and pairwise interactions (f_i * f_j, i < j).
Fits via OLS and reports coefficients, R², nonlinear significance
(approximate F-test on squared + interaction terms), and a
linear-vs-quadratic model comparison.

Pure Python, deterministic. Frozen dataclass result with to_dict().
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "QuadraticFactorModelResult",
    "quadratic_factor_model_report",
]

_MAX_SERIES = 5000
_MAX_FACTORS = 20


@dataclass(frozen=True)
class QuadraticFactorModelResult:
    coefficients: dict[str, float]
    r_squared: float
    nonlinear_significance: float
    linear_vs_quadratic_comparison: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "coefficients": self.coefficients,
            "r_squared": self.r_squared,
            "nonlinear_significance": self.nonlinear_significance,
            "linear_vs_quadratic_comparison": self.linear_vs_quadratic_comparison,
        }


def _validate_series(values: Any, name: str, min_len: int = 1) -> list[float]:
    if not isinstance(values, list):
        raise ValueError(f"{name} must be a non-empty list of finite numbers")
    if not values:
        raise ValueError(f"{name} must be a non-empty list of finite numbers")
    if len(values) > _MAX_SERIES:
        raise ValueError(f"{name} must contain at most {_MAX_SERIES} values")
    if len(values) < min_len:
        raise ValueError(f"{name} must contain at least {min_len} values")
    out: list[float] = []
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(f"{name} entries must be finite numbers")
        number = float(v)
        if not math.isfinite(number):
            raise ValueError(f"{name} entries must be finite numbers")
        out.append(number)
    return out


def _validate_factors(factors: dict[str, list[float]], n: int) -> dict[str, list[float]]:
    if not isinstance(factors, dict):
        raise ValueError("factors must be a non-empty dict")
    if not factors:
        raise ValueError("factors must be a non-empty dict")
    if len(factors) > _MAX_FACTORS:
        raise ValueError(f"factors must contain at most {_MAX_FACTORS} entries")
    validated: dict[str, list[float]] = {}
    for name, series in factors.items():
        if not isinstance(name, str) or not name:
            raise ValueError("factor names must be non-empty strings")
        vals = _validate_series(series, f"factor '{name}'")
        if len(vals) != n:
            raise ValueError(f"factor '{name}' must have length = {n} (same as returns)")
        validated[name] = vals
    return validated


# ---------------------------------------------------------------------------
# Pure-Python OLS: solve Xβ = y via normal equations + Gaussian elimination
# ---------------------------------------------------------------------------


def _matrix_multiply(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    """Multiply matrices A (m×k) × B (k×n) → C (m×n)."""
    m = len(A)
    k = len(A[0]) if A else 0
    n = len(B[0]) if B else 0
    C = [[0.0] * n for _ in range(m)]
    for i in range(m):
        for j in range(n):
            total = 0.0
            for p in range(k):
                total += A[i][p] * B[p][j]
            C[i][j] = total
    return C


def _transpose(X: list[list[float]]) -> list[list[float]]:
    if not X:
        return []
    return [[X[i][j] for i in range(len(X))] for j in range(len(X[0]))]


def _gaussian_elimination(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve Ax = b via Gaussian elimination with partial pivoting.

    Modifies A and b in place. Returns solution vector x.
    Raises ValueError if the system is singular.
    """
    n = len(A)
    # Augment
    M = [A[i][:] + [b[i]] for i in range(n)]

    for col in range(n):
        # Partial pivoting
        max_row = max(range(col, n), key=lambda r: abs(M[r][col]))
        if abs(M[max_row][col]) < 1e-15:
            raise ValueError("singular matrix in OLS")
        if max_row != col:
            M[col], M[max_row] = M[max_row], M[col]

        # Eliminate below
        pivot = M[col][col]
        for row in range(col + 1, n):
            factor = M[row][col] / pivot
            for j in range(col, n + 1):
                M[row][j] -= factor * M[col][j]

    # Back substitution
    x = [0.0] * n
    for i in range(n - 1, -1, -1):
        total = M[i][n]
        for j in range(i + 1, n):
            total -= M[i][j] * x[j]
        x[i] = total / M[i][i]
    return x


def _ols(X: list[list[float]], y: list[float]) -> tuple[list[float], float]:
    """Ordinary least squares: solve β = (XᵀX)⁻¹Xᵀy.

    Args:
        X: Design matrix, n rows × p columns.
        y: Response vector, length n.

    Returns:
        (coefficients β, R²)

    Raises:
        ValueError: If the system is singular.
    """
    Xt = _transpose(X)
    XtX = _matrix_multiply(Xt, X)
    Xty = [sum(Xt[i][j] * y[j] for j in range(len(y))) for i in range(len(Xt))]

    try:
        beta = _gaussian_elimination(XtX, Xty)
    except ValueError:
        raise ValueError("design matrix is singular")

    # Compute R²
    y_mean = sum(y) / len(y)
    ss_total = sum((v - y_mean) ** 2 for v in y)
    if ss_total == 0:
        r_squared = 1.0
    else:
        y_hat = [sum(X[i][j] * beta[j] for j in range(len(beta))) for i in range(len(X))]
        ss_res = sum((y[i] - y_hat[i]) ** 2 for i in range(len(y)))
        r_squared = 1.0 - ss_res / ss_total

    return beta, r_squared


# ---------------------------------------------------------------------------
# Main function
# ---------------------------------------------------------------------------


def quadratic_factor_model_report(
    returns: list[float],
    factors: dict[str, list[float]],
) -> QuadraticFactorModelResult:
    """Quadratic factor model: OLS regression of returns on linear + squared + interaction terms.

    Args:
        returns: Asset return series.
        factors: Factor return series, each of same length as returns.

    Returns:
        QuadraticFactorModelResult with coefficients, R², nonlinear significance
        (approximate F-statistic for squared/interaction terms), and a comparison
        between linear-only and full quadratic models.

    Raises:
        ValueError: On invalid input (non-list, mismatched lengths, singular design).
    """
    returns_v = _validate_series(returns, "returns", min_len=3)
    n = len(returns_v)

    factor_names = sorted(_validate_factors(factors, n).keys())
    factor_data = {name: factors[name] for name in factor_names}

    # Build term names and design matrix columns
    term_names: list[str] = []
    X_cols: list[list[float]] = []

    # Constant
    term_names.append("const")
    X_cols.append([1.0] * n)

    # Linear terms
    for name in factor_names:
        term_names.append(name)
        X_cols.append(factor_data[name])

    # Squared terms
    for name in factor_names:
        term_names.append(f"{name}^2")
        X_cols.append([v * v for v in factor_data[name]])

    # Interaction terms (i < j)
    for i in range(len(factor_names)):
        for j in range(i + 1, len(factor_names)):
            fi = factor_data[factor_names[i]]
            fj = factor_data[factor_names[j]]
            term_names.append(f"{factor_names[i]}*{factor_names[j]}")
            X_cols.append([fi[k] * fj[k] for k in range(n)])

    # Build design matrix (n × p, column-major → transpose to row-major)
    X = [[X_cols[j][i] for j in range(len(X_cols))] for i in range(n)]

    try:
        beta, r_squared = _ols(X, returns_v)
    except ValueError as exc:
        raise ValueError(f"OLS failed: {exc}")

    coefficients = {term_names[j]: beta[j] for j in range(len(beta))}

    # Linear-only model for comparison
    linear_term_indices = [j for j, tn in enumerate(term_names) if tn == "const" or (not tn.endswith("^2") and "*" not in tn and tn != "const")]
    linear_names = [term_names[j] for j in linear_term_indices]
    X_linear = [[X_cols[j][i] for j in linear_term_indices] for i in range(n)]
    beta_linear, r_squared_linear = _ols(X_linear, returns_v)

    linear_vs_quadratic_comparison = {
        "linear_r_squared": r_squared_linear,
        "quadratic_r_squared": r_squared,
        "r_squared_improvement": r_squared - r_squared_linear,
    }

    # Approximate nonlinear significance: F-test on squared + interaction terms
    # F = ((SS_linear - SS_quadratic) / df_extra) / (SS_quadratic / df_quadratic)
    y_mean = sum(returns_v) / n
    ss_total = sum((v - y_mean) ** 2 for v in returns_v)
    ss_res_full = ss_total * (1.0 - r_squared) if r_squared < 1.0 else 0.0
    ss_res_linear = ss_total * (1.0 - r_squared_linear) if r_squared_linear < 1.0 else 0.0

    df_linear = len(linear_term_indices)
    df_quadratic = len(term_names)
    df_extra = df_quadratic - df_linear
    df_resid = n - df_quadratic

    if df_resid > 0 and df_extra > 0 and ss_res_full > 0:
        f_stat = ((ss_res_linear - ss_res_full) / df_extra) / (ss_res_full / df_resid)
    else:
        f_stat = 0.0

    return QuadraticFactorModelResult(
        coefficients=coefficients,
        r_squared=r_squared,
        nonlinear_significance=abs(f_stat),
        linear_vs_quadratic_comparison=linear_vs_quadratic_comparison,
    )
