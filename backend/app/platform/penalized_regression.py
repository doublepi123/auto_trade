"""P350: Penalized regression (Ridge / Lasso).

Pure Python implementation of ridge regression (closed-form) and lasso
regression (coordinate descent). No scipy/numpy dependency.

References
----------
* Hoerl & Kennard (1970) "Ridge Regression"
* Tibshirani (1996) "Regression Shrinkage and Selection via the Lasso"
* Friedman, Hastie & Tibshirani (2010) "Regularization Paths"
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "PenalizedRegressionResult",
    "penalized_regression_report",
]


def _validate(y: list[float], x: list[list[float]]) -> int:
    """Validate inputs and return n (number of observations)."""
    if not y:
        raise ValueError("y must be non-empty")
    if not x:
        raise ValueError("x must be non-empty")
    n = len(y)
    k = len(x)
    if n < 2:
        raise ValueError("need at least 2 observations")
    for i, series in enumerate(x):
        if len(series) != n:
            raise ValueError(f"x[{i}] length must equal y length ({n})")
    for val in y:
        if not math.isfinite(val):
            raise ValueError("y must contain finite numbers")
    for series in x:
        for val in series:
            if not math.isfinite(val):
                raise ValueError("x must contain finite numbers")
    return n


def _standardize(
    y: list[float], x: list[list[float]]
) -> tuple[list[float], list[list[float]], float, list[float], list[float]]:
    """Standardize y and x columns to zero mean.

    Returns (y_std, x_std, y_mean, x_means, x_stds).
    """
    n = len(y)
    k = len(x)
    y_mean = sum(y) / n
    y_std = [yi - y_mean for yi in y]

    x_means: list[float] = []
    x_stds: list[float] = []
    x_std: list[list[float]] = []

    for j in range(k):
        mean_j = sum(x[j]) / n
        x_means.append(mean_j)
        std_j = max(math.sqrt(sum((xi - mean_j) ** 2 for xi in x[j]) / n), 1e-15)
        x_stds.append(std_j)
        x_std.append([(x[j][i] - mean_j) / std_j for i in range(n)])

    return y_std, x_std, y_mean, x_means, x_stds


def _matrix_multiply_transpose(A: list[list[float]], B: list[list[float]]) -> list[list[float]]:
    """Compute A @ B^T where A is (k, n) and B is (k, n), returns (k, k)."""
    k = len(A)
    n = len(A[0])
    result: list[list[float]] = []
    for i in range(k):
        row: list[float] = []
        for j in range(k):
            dot = sum(A[i][t] * B[j][t] for t in range(n))
            row.append(dot)
        result.append(row)
    return result


def _matrix_vector_multiply(A: list[list[float]], v: list[float]) -> list[float]:
    """Compute A @ v where A is (k, n) and v is (n,)."""
    k = len(A)
    n = len(A[0])
    return [sum(A[i][t] * v[t] for t in range(n)) for i in range(k)]


def _matrix_vector_multiply_square(A: list[list[float]], v: list[float]) -> list[float]:
    """Compute A @ v where A is (d, d) and v is (d,)."""
    d = len(A)
    return [sum(A[i][j] * v[j] for j in range(d)) for i in range(d)]


def _solve_cholesky(L: list[list[float]], b: list[float]) -> list[float]:
    """Solve L @ L^T @ x = b via forward/back substitution."""
    d = len(L)
    # Forward: L @ y = b
    y = [0.0] * d
    for i in range(d):
        s = b[i]
        for j in range(i):
            s -= L[i][j] * y[j]
        y[i] = s / L[i][i]
    # Backward: L^T @ x = y
    x = [0.0] * d
    for i in range(d - 1, -1, -1):
        s = y[i]
        for j in range(i + 1, d):
            s -= L[j][i] * x[j]
        x[i] = s / L[i][i]
    return x


def _cholesky(A: list[list[float]]) -> list[list[float]]:
    """Cholesky decomposition A = L @ L^T, returns L (lower triangular)."""
    d = len(A)
    L: list[list[float]] = [[0.0] * d for _ in range(d)]
    for i in range(d):
        for j in range(i + 1):
            s = A[i][j]
            for k in range(j):
                s -= L[i][k] * L[j][k]
            if i == j:
                if s <= 0.0:
                    raise ValueError("matrix is not positive definite")
                L[i][j] = math.sqrt(s)
            else:
                L[i][j] = s / L[j][j]
    return L


def _ridge_solve(
    x_std: list[list[float]], y_std: list[float], alpha: float
) -> list[float]:
    """Closed-form ridge solution: β = (X^T X + αI)^-1 X^T y."""
    k = len(x_std)
    # X^T X
    xtx = _matrix_multiply_transpose(x_std, x_std)
    # X^T y
    xty = _matrix_vector_multiply(x_std, y_std)

    # Add alpha to diagonal of xtx
    for i in range(k):
        xtx[i][i] += alpha

    # Solve via Cholesky
    L = _cholesky(xtx)
    beta = _solve_cholesky(L, xty)
    return beta


def _lasso_coordinate_descent(
    x_std: list[list[float]], y_std: list[float], alpha: float, max_iter: int = 100
) -> list[float]:
    """Coordinate descent for lasso (L1-penalized) regression.

    Minimizes: 0.5 * ||y - Xβ||² + α * Σ|β_j|
    """
    k = len(x_std)
    n = len(y_std)

    # Pre-compute X^T X diagonal and X^T y
    xtx_diag = [sum(x_std[j][i] ** 2 for i in range(n)) for j in range(k)]
    xty = _matrix_vector_multiply(x_std, y_std)

    # Initialize β = 0
    beta = [0.0] * k

    for _iter in range(max_iter):
        for j in range(k):
            # Residual contribution from other coefficients
            residual = 0.0
            for t in range(k):
                if t != j and abs(beta[t]) > 0.0:
                    # Compute X_j^T X_t
                    dot_jt = sum(x_std[j][i] * x_std[t][i] for i in range(n))
                    residual += dot_jt * beta[t]

            rho_j = xty[j] - residual
            # Soft thresholding
            if rho_j > alpha:
                beta[j] = (rho_j - alpha) / xtx_diag[j]
            elif rho_j < -alpha:
                beta[j] = (rho_j + alpha) / xtx_diag[j]
            else:
                beta[j] = 0.0

    return beta


def _predict(x_std: list[list[float]], beta: list[float]) -> list[float]:
    """Predict y from standardized x and beta."""
    k = len(x_std)
    n = len(x_std[0])
    return [sum(beta[j] * x_std[j][i] for j in range(k)) for i in range(n)]


def _r_squared(y_true: list[float], y_pred: list[float]) -> float:
    """Compute R-squared."""
    n = len(y_true)
    y_mean = sum(y_true) / n
    ss_res = sum((y_true[i] - y_pred[i]) ** 2 for i in range(n))
    ss_tot = sum((y_true[i] - y_mean) ** 2 for i in range(n))
    if ss_tot < 1e-15:
        return 1.0 if ss_res < 1e-15 else 0.0
    return 1.0 - ss_res / ss_tot


@dataclass(frozen=True)
class PenalizedRegressionResult:
    method: str
    coefficients: dict[str, float]
    r_squared: float
    residual_std: float

    def to_dict(self) -> dict:
        return {
            "method": self.method,
            "coefficients": self.coefficients,
            "r_squared": self.r_squared,
            "residual_std": self.residual_std,
        }


def penalized_regression_report(
    y: list[float],
    x: list[list[float]],
    *,
    method: str = "ridge",
    alpha: float = 1.0,
    max_iter: int = 100,
) -> PenalizedRegressionResult:
    """Penalized regression (Ridge or Lasso) report.

    Parameters
    ----------
    y : list[float]
        Target variable, length n.
    x : list[list[float]]
        List of k regressor series, each of length n.
    method : "ridge" or "lasso"
        Penalty type.
    alpha : float
        Regularization strength (>= 0).
    max_iter : int
        Maximum iterations for coordinate descent (lasso only).

    Returns
    -------
    PenalizedRegressionResult
        Frozen dataclass with method, coefficients (incl. intercept),
        R-squared, and residual standard deviation.
    """
    if method not in ("ridge", "lasso"):
        raise ValueError("method must be 'ridge' or 'lasso'")
    if alpha < 0.0 or not math.isfinite(alpha):
        raise ValueError("alpha must be a finite non-negative number")

    n = _validate(y, x)
    y_std, x_std, y_mean, x_means, x_stds = _standardize(y, x)

    if method == "ridge":
        beta_std = _ridge_solve(x_std, y_std, alpha)
    else:
        beta_std = _lasso_coordinate_descent(x_std, y_std, alpha, max_iter)

    # Predict on standardized scale
    y_pred_std = _predict(x_std, beta_std)

    # De-standardize predictions: y_pred = y_pred_std + y_mean
    y_pred = [yp + y_mean for yp in y_pred_std]

    # Compute R-squared on original scale
    rsq = _r_squared(y, y_pred)

    # Residual std on original scale
    residuals = [y[i] - y_pred[i] for i in range(n)]
    residual_var = sum(r * r for r in residuals) / (n - len(x) - 1) if n > len(x) + 1 else sum(r * r for r in residuals) / max(n - 1, 1)
    residual_std = math.sqrt(max(residual_var, 0.0))

    # Convert coefficients back to original scale
    # β_j_original = β_j_std / std_j
    # intercept = y_mean - Σ(β_j_original * x_mean_j)
    coefficients: dict[str, float] = {}
    intercept = y_mean
    for j in range(len(x)):
        beta_orig = beta_std[j] / x_stds[j]
        coefficients[f"b{j + 1}"] = beta_orig
        intercept -= beta_orig * x_means[j]
    coefficients["intercept"] = intercept

    return PenalizedRegressionResult(
        method=method,
        coefficients=coefficients,
        r_squared=rsq,
        residual_std=residual_std,
    )
