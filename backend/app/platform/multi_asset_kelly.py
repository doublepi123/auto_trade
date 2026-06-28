"""P351: Multi-asset Kelly portfolio allocation.

Compute optimal Kelly weights for a portfolio of assets from a returns panel.
Supports fractional Kelly scaling to reduce leverage and risk.

Pure Python, no scipy/numpy. References: Kelly (1956), Thorp (1969),
MacLean, Thorp & Ziemba (2011) "The Kelly Capital Growth Investment Criterion".
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "MultiAssetKellyResult",
    "multi_asset_kelly_report",
]


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


def _solve_cholesky(L: list[list[float]], b: list[float]) -> list[float]:
    """Solve L @ L^T @ x = b via forward/back substitution."""
    d = len(L)
    y = [0.0] * d
    for i in range(d):
        s = b[i]
        for j in range(i):
            s -= L[i][j] * y[j]
        y[i] = s / L[i][i]
    x = [0.0] * d
    for i in range(d - 1, -1, -1):
        s = y[i]
        for j in range(i + 1, d):
            s -= L[j][i] * x[j]
        x[i] = s / L[i][i]
    return x


def _solve_linear(A: list[list[float]], b: list[float]) -> list[float]:
    """Solve A @ x = b for symmetric positive-definite A via Cholesky."""
    L = _cholesky(A)
    return _solve_cholesky(L, b)


def _compute_covariance(
    panel: dict[str, list[float]],
) -> tuple[list[float], list[list[float]], int, list[str]]:
    """Compute sample mean vector and covariance matrix from a returns panel.

    Returns (mu, cov, n, symbols).
    """
    symbols = list(panel.keys())
    k = len(symbols)
    n = len(panel[symbols[0]])

    # Sample means
    mu = [sum(panel[s]) / n for s in symbols]

    # Sample covariance: Cov[i][j] = Σ (r_i_t - μ_i) * (r_j_t - μ_j) / (n - 1)
    cov: list[list[float]] = [[0.0] * k for _ in range(k)]
    for i in range(k):
        ri = panel[symbols[i]]
        for j in range(i, k):
            rj = panel[symbols[j]]
            s = sum((ri[t] - mu[i]) * (rj[t] - mu[j]) for t in range(n))
            cov[i][j] = s / (n - 1)
            if i != j:
                cov[j][i] = cov[i][j]

    return mu, cov, n, symbols


@dataclass(frozen=True)
class MultiAssetKellyResult:
    kelly_weights: dict[str, float]
    fractional_weights: dict[str, float]
    expected_growth_rate: float
    leverage: float
    fraction: float

    def to_dict(self) -> dict:
        return {
            "kelly_weights": self.kelly_weights,
            "fractional_weights": self.fractional_weights,
            "expected_growth_rate": self.expected_growth_rate,
            "leverage": self.leverage,
            "fraction": self.fraction,
        }


def multi_asset_kelly_report(
    returns_panel: dict[str, list[float]],
    *,
    fraction: float = 1.0,
) -> MultiAssetKellyResult:
    """Multi-asset Kelly portfolio allocation report.

    Parameters
    ----------
    returns_panel : dict[str, list[float]]
        Panel of asset returns, keyed by symbol. All series must be equal
        length and non-empty. At least 2 observations required.
    fraction : float
        Fractional Kelly scaling factor in (0, 1]. Default 1.0 (full Kelly).

    Returns
    -------
    MultiAssetKellyResult
        Frozen dataclass with kelly_weights, fractional_weights,
        expected_growth_rate, leverage, and fraction.
    """
    # Validate fraction
    if not (0.0 < fraction <= 1.0):
        raise ValueError("fraction must be in (0, 1]")

    # Validate panel
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")
    if len(returns_panel) > 50:
        raise ValueError("returns_panel must contain at most 50 assets")
    for name, series in returns_panel.items():
        if not isinstance(series, list) or not series:
            raise ValueError(f"returns_panel['{name}'] must be a non-empty list")
        for val in series:
            if not math.isfinite(val):
                raise ValueError(f"returns_panel['{name}'] must contain finite numbers")

    # Check equal lengths
    lengths = {len(s) for s in returns_panel.values()}
    if len(lengths) != 1:
        raise ValueError("all series in returns_panel must have equal length")

    n = lengths.pop()
    if n < 2:
        raise ValueError("each series must have at least 2 observations")

    mu, cov, _, symbols = _compute_covariance(returns_panel)
    k = len(symbols)

    # Check if all covariances are zero
    all_zero = True
    for i in range(k):
        for j in range(k):
            if abs(cov[i][j]) > 1e-15:
                all_zero = False
                break
        if not all_zero:
            break

    if all_zero:
        # Zero-variance case: return zero weights
        kelly_w = {s: 0.0 for s in symbols}
        frac_w = {s: 0.0 for s in symbols}
        return MultiAssetKellyResult(
            kelly_weights=kelly_w,
            fractional_weights=frac_w,
            expected_growth_rate=0.0,
            leverage=0.0,
            fraction=fraction,
        )

    # Solve w* = Cov^{-1} @ mu
    # Add small ridge to diagonal for numerical stability
    max_diag = max(cov[i][i] for i in range(k))
    ridge = max(max_diag * 1e-8, 1e-12)
    cov_reg = [[cov[i][j] for j in range(k)] for i in range(k)]
    for i in range(k):
        cov_reg[i][i] += ridge

    try:
        w_star = _solve_linear(cov_reg, mu)
    except ValueError:
        # Fallback: equal-weight with zero growth
        w_star = [0.0] * k

    # Fractional weights
    w_frac = [wi * fraction for wi in w_star]

    # Expected growth rate: fraction * mu^T @ w* - 0.5 * fraction^2 * w*^T @ Cov @ w*
    # = fraction * Σ μ_i w*_i - 0.5 * fraction^2 * Σ_i Σ_j w*_i Cov_ij w*_j
    mu_dot_w = sum(mu[i] * w_star[i] for i in range(k))
    w_cov_w = sum(
        sum(w_star[i] * cov[i][j] * w_star[j] for j in range(k))
        for i in range(k)
    )
    growth = fraction * mu_dot_w - 0.5 * (fraction ** 2) * w_cov_w
    growth = max(growth, 0.0)

    # Leverage = sum of absolute fractional weights
    leverage = sum(abs(wi) for wi in w_frac)

    kelly_weights = {symbols[i]: w_star[i] for i in range(k)}
    fractional_weights = {symbols[i]: w_frac[i] for i in range(k)}

    return MultiAssetKellyResult(
        kelly_weights=kelly_weights,
        fractional_weights=fractional_weights,
        expected_growth_rate=growth,
        leverage=leverage,
        fraction=fraction,
    )
