"""P334: Online (EWMA) covariance estimation.

Recursive exponentially-weighted moving average covariance estimator.
Initialized with the sample covariance of the first ``min_window`` observations,
then updated point-by-point via:

    cov_t = λ · cov_{t-1} + (1 − λ) · r_t · r_tᵀ

Returns the latest covariance matrix (as a nested dict), the condition number
(max / min eigenvalue), and the full eigenvalue list. Eigen-decomposition uses
the cyclic Jacobi method (same algorithm as the PCA module). Pure Python,
no numpy/scipy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

__all__ = ["OnlineCovarianceResult", "online_covariance_report"]


@dataclass(frozen=True)
class OnlineCovarianceResult:
    latest_covariance: dict[str, dict[str, float]] = field(default_factory=dict)
    condition_number: float = 0.0
    eigenvalues: list[float] = field(default_factory=list)
    assets: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "latest_covariance": self.latest_covariance,
            "condition_number": self.condition_number,
            "eigenvalues": self.eigenvalues,
            "assets": self.assets,
        }


def _sample_cov_matrix(columns: list[list[float]]) -> list[list[float]]:
    """Compute n×n sample covariance from a list of n column vectors."""
    n_assets = len(columns)
    if n_assets == 0:
        return []
    n = len(columns[0])
    if n < 2:
        return [[0.0] * n_assets for _ in range(n_assets)]
    means = [sum(col) / n for col in columns]
    cov = [[0.0] * n_assets for _ in range(n_assets)]
    for i in range(n_assets):
        for j in range(i, n_assets):
            acc = 0.0
            for k in range(n):
                acc += (columns[i][k] - means[i]) * (columns[j][k] - means[j])
            val = acc / (n - 1)
            cov[i][j] = val
            cov[j][i] = val
    return cov


def _jacobi_eigen(cov: list[list[float]], *, max_iter: int = 100, tol: float = 1e-12) -> list[float]:
    """Compute eigenvalues of a real symmetric matrix via cyclic Jacobi.

    Returns eigenvalues sorted in descending order. The matrix is modified in-place.
    """
    n = len(cov)
    if n == 0:
        return []
    if n == 1:
        return [cov[0][0]]

    for _ in range(max_iter):
        # Find the largest off-diagonal element
        max_abs = 0.0
        p, q = 0, 1
        for i in range(n):
            for j in range(i + 1, n):
                val = abs(cov[i][j])
                if val > max_abs:
                    max_abs = val
                    p, q = i, j
        if max_abs < tol:
            break

        # Compute rotation
        app = cov[p][p]
        aqq = cov[q][q]
        apq = cov[p][q]
        theta = 0.5 * math.atan2(2.0 * apq, app - aqq) if abs(app - aqq) > 1e-15 else (math.pi / 4.0 if apq > 0 else -math.pi / 4.0)
        c = math.cos(theta)
        s = math.sin(theta)

        # Apply rotation
        cov[p][p] = app * c * c + aqq * s * s + 2.0 * apq * s * c
        cov[q][q] = app * s * s + aqq * c * c - 2.0 * apq * s * c
        cov[p][q] = 0.0
        cov[q][p] = 0.0

        for i in range(n):
            if i != p and i != q:
                a_ip = cov[i][p]
                a_iq = cov[i][q]
                cov[i][p] = a_ip * c + a_iq * s
                cov[p][i] = cov[i][p]
                cov[i][q] = -a_ip * s + a_iq * c
                cov[q][i] = cov[i][q]

    # Extract diagonal (eigenvalues)
    evals = [cov[i][i] for i in range(n)]
    evals.sort(reverse=True)
    return evals


def online_covariance_report(
    returns_panel: dict[str, list[float]],
    *,
    lam: float = 0.97,
    min_window: int = 30,
) -> OnlineCovarianceResult:
    """Online EWMA covariance estimation.

    Computes the latest covariance matrix via exponential weighting, starting
    from the sample covariance over the first ``min_window`` observations.

    Args:
        returns_panel: {asset_name: return_series} dict. All series must be
            equal-length with at least ``min_window`` observations.
        lam: EWMA decay factor in (0, 1]. Default 0.97.
        min_window: Minimum number of observations for initial sample covariance.
            Default 30.

    Returns:
        OnlineCovarianceResult with latest covariance, condition number,
        eigenvalues, and asset names.

    Raises:
        ValueError: If inputs are invalid.
    """
    if not returns_panel or not isinstance(returns_panel, dict):
        raise ValueError("returns_panel must be a non-empty dict")
    assets = list(returns_panel.keys())
    if len(assets) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    if not math.isfinite(lam) or lam <= 0 or lam > 1.0:
        raise ValueError("lam must be in (0, 1]")
    if not isinstance(min_window, int) or min_window < 2:
        raise ValueError("min_window must be an integer >= 2")

    # Validate all series
    n = len(returns_panel[assets[0]])
    if n < min_window:
        raise ValueError(f"returns_panel series must have at least {min_window} observations, got {n}")
    for a in assets:
        s = returns_panel[a]
        if not isinstance(s, list) or len(s) != n:
            raise ValueError(f"series for '{a}' must be a list of length {n}")
        for v in s:
            if not math.isfinite(v):
                raise ValueError(f"series for '{a}' contains non-finite values")

    n_assets = len(assets)

    # Build aligned column vectors
    columns = [[float(returns_panel[a][i]) for i in range(n)] for a in assets]

    # Initialize with sample covariance over first min_window obs
    init_cols = [[columns[ai][i] for i in range(min_window)] for ai in range(n_assets)]
    cov_mat = _sample_cov_matrix(init_cols)

    # EWMA update for remaining observations
    one_minus_lam = 1.0 - lam
    for t in range(min_window, n):
        r_t = [columns[ai][t] for ai in range(n_assets)]
        for i in range(n_assets):
            for j in range(i, n_assets):
                outer = r_t[i] * r_t[j]
                new_val = lam * cov_mat[i][j] + one_minus_lam * outer
                cov_mat[i][j] = new_val
                cov_mat[j][i] = new_val

    # Convert to nested dict
    latest_cov: dict[str, dict[str, float]] = {}
    for i in range(n_assets):
        latest_cov[assets[i]] = {}
        for j in range(n_assets):
            latest_cov[assets[i]][assets[j]] = cov_mat[i][j]

    # Compute eigenvalues
    # Make a copy for Jacobi since it modifies in-place
    cov_copy = [row[:] for row in cov_mat]
    eigenvalues = _jacobi_eigen(cov_copy)

    # Condition number
    if eigenvalues and len(eigenvalues) >= 2 and eigenvalues[-1] > 1e-15:
        condition_number = eigenvalues[0] / eigenvalues[-1]
    elif eigenvalues and eigenvalues[-1] <= 1e-15 and eigenvalues[0] > 0:
        condition_number = float("inf")
    else:
        condition_number = 1.0

    return OnlineCovarianceResult(
        latest_covariance=latest_cov,
        condition_number=condition_number,
        eigenvalues=eigenvalues,
        assets=assets,
    )
