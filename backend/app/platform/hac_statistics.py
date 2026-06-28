"""P330: HAC (Newey-West) standard errors for OLS regression."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, validate_series


@dataclass(frozen=True)
class HacStatisticsResult:
    coefficients: dict[str, float]
    hac_std_errors: dict[str, float]
    hac_t_stats: dict[str, float]
    hac_p_values: dict[str, float]
    ols_std_errors: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "coefficients": dict(self.coefficients),
            "hac_std_errors": dict(self.hac_std_errors),
            "hac_t_stats": dict(self.hac_t_stats),
            "hac_p_values": dict(self.hac_p_values),
            "ols_std_errors": dict(self.ols_std_errors),
        }


def hac_statistics_report(
    y: list[float],
    x: list[list[float]],
    *,
    lags: int = 5,
) -> HacStatisticsResult:
    # Validate y
    y_vals = validate_series(y, name="y", min_len=2)
    n = len(y_vals)

    if not isinstance(x, list):
        raise ValueError("x must be a list of regressor series")
    k_reg = len(x)
    if k_reg == 0:
        raise ValueError("x must contain at least one regressor series")

    x_vals: list[list[float]] = []
    for idx, xi in enumerate(x):
        xv = validate_series(xi, name=f"x[{idx}]", min_len=2)
        if len(xv) != n:
            raise ValueError("y and all x series must have the same length")
        x_vals.append(xv)

    if isinstance(lags, bool) or not isinstance(lags, int):
        raise ValueError("lags must be an int >= 0")
    if lags < 0:
        raise ValueError("lags must be >= 0")

    k = k_reg + 1  # total params including intercept
    if n <= k:
        raise ValueError(f"need n > k (got n={n}, k={k})")
    if lags >= n:
        raise ValueError(f"lags ({lags}) must be < n ({n})")

    # Build design matrix X: [const, x_0, x_1, ...]
    # X is n × (k_reg + 1)
    X: list[list[float]] = []
    for i in range(n):
        row = [1.0] + [x_vals[j][i] for j in range(k_reg)]
        X.append(row)

    # OLS: β = (X'X)^-1 X'y
    # Compute X'X (k × k)
    XtX = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            s = 0.0
            for t in range(n):
                s += X[t][i] * X[t][j]
            XtX[i][j] = s

    # Invert X'X using Gaussian elimination (k is small: 1 + #regressors)
    XtX_inv = _matrix_invert(XtX)

    # Compute β = XtX_inv * X'y
    Xty = [0.0] * k
    for i in range(k):
        s = 0.0
        for t in range(n):
            s += X[t][i] * y_vals[t]
        Xty[i] = s

    beta = [0.0] * k
    for i in range(k):
        s = 0.0
        for j in range(k):
            s += XtX_inv[i][j] * Xty[j]
        beta[i] = s

    # Residuals
    residuals = [0.0] * n
    for t in range(n):
        pred = 0.0
        for j in range(k):
            pred += X[t][j] * beta[j]
        residuals[t] = y_vals[t] - pred

    # Compute residual variance (OLS)
    df = n - k
    rss = sum(e * e for e in residuals)
    sigma2 = rss / df if df > 0 else 0.0

    # OLS standard errors: sqrt(sigma2 * diag(XtX_inv))
    ols_se = [0.0] * k
    for i in range(k):
        ols_se[i] = max(0.0, math.sqrt(sigma2 * XtX_inv[i][i]))

    # HAC (Newey-West) covariance
    # S = S_0 + sum_{j=1}^{lags} w_j * (S_j + S_j')
    # w_j = 1 - j/(lags+1)
    # S_j = (1/n) * sum_{t=j+1}^{n} e_t * e_{t-j} * (X_t' ⊗ X_{t-j})
    # HAC cov = (X'X)^-1 * S * (X'X)^-1  (need n factor adjustment)

    # Compute n * S (scaled version)
    S = [[0.0] * k for _ in range(k)]

    # S_0 contribution: sum_t e_t^2 * X_t' ⊗ X_t
    for t in range(n):
        et2 = residuals[t] * residuals[t]
        for i in range(k):
            for j in range(k):
                S[i][j] += et2 * X[t][i] * X[t][j]

    # Lag contributions
    for lag in range(1, lags + 1):
        w = 1.0 - lag / (lags + 1.0)
        # S_j
        Sj = [[0.0] * k for _ in range(k)]
        for t in range(lag, n):
            prod = residuals[t] * residuals[t - lag]
            for i in range(k):
                for j in range(k):
                    Sj[i][j] += prod * X[t][i] * X[t - lag][j]
        # Add w * (Sj + Sj')
        for i in range(k):
            for j in range(k):
                S[i][j] += w * (Sj[i][j] + Sj[j][i])

    # HAC covariance: 1/n * XtX_inv * S * XtX_inv
    # First compute temp = S * XtX_inv
    # Note: S above is already scaled (sum over t), so we need to divide by n
    n_recip = 1.0 / n
    hac_cov = [[0.0] * k for _ in range(k)]
    for i in range(k):
        for j in range(k):
            s = 0.0
            for u in range(k):
                for v in range(k):
                    s += XtX_inv[i][u] * S[u][v] * XtX_inv[v][j]
            hac_cov[i][j] = s * n_recip

    # HAC standard errors
    hac_se = [0.0] * k
    for i in range(k):
        hac_se[i] = max(0.0, math.sqrt(hac_cov[i][i]))

    # t-stats and p-values (two-tailed normal approximation)
    hac_t = [0.0] * k
    hac_p = [1.0] * k
    for i in range(k):
        if hac_se[i] > 0:
            hac_t[i] = beta[i] / hac_se[i]
            # erfc(|t| / sqrt(2)) gives two-tailed p-value
            hac_p[i] = math.erfc(abs(hac_t[i]) / math.sqrt(2))
        else:
            hac_t[i] = 0.0
            hac_p[i] = 1.0

    # Build output dicts with names
    coefs: dict[str, float] = {"const": beta[0]}
    hac_se_dict: dict[str, float] = {"const": hac_se[0]}
    hac_t_dict: dict[str, float] = {"const": hac_t[0]}
    hac_p_dict: dict[str, float] = {"const": hac_p[0]}
    ols_se_dict: dict[str, float] = {"const": ols_se[0]}

    for j in range(k_reg):
        key = f"x_{j}"
        coefs[key] = beta[j + 1]
        hac_se_dict[key] = hac_se[j + 1]
        hac_t_dict[key] = hac_t[j + 1]
        hac_p_dict[key] = hac_p[j + 1]
        ols_se_dict[key] = ols_se[j + 1]

    return HacStatisticsResult(
        coefficients=coefs,
        hac_std_errors=hac_se_dict,
        hac_t_stats=hac_t_dict,
        hac_p_values=hac_p_dict,
        ols_std_errors=ols_se_dict,
    )


def _matrix_invert(A: list[list[float]]) -> list[list[float]]:
    """Invert a square matrix via Gaussian elimination (for small matrices)."""
    n = len(A)
    # Augment with identity
    augmented = [row[:] + [1.0 if i == j else 0.0 for j in range(n)] for i, row in enumerate(A)]

    for col in range(n):
        # Find pivot
        pivot_row = max(range(col, n), key=lambda r: abs(augmented[r][col]))
        if abs(augmented[pivot_row][col]) < 1e-15:
            raise ValueError("matrix is singular or nearly singular")
        if pivot_row != col:
            augmented[col], augmented[pivot_row] = augmented[pivot_row], augmented[col]
        # Normalize pivot row
        pivot_val = augmented[col][col]
        for j in range(2 * n):
            augmented[col][j] /= pivot_val
        # Eliminate other rows
        for row in range(n):
            if row != col:
                factor = augmented[row][col]
                for j in range(2 * n):
                    augmented[row][j] -= factor * augmented[col][j]

    # Extract the right half
    return [[augmented[i][n + j] for j in range(n)] for i in range(n)]
