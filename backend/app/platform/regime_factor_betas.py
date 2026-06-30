"""P386: Regime-factor beta analysis.

Slice a return series by regime labels, then within each regime run an OLS
regression of returns on factor returns.  Summarises the cross-regime
stability of factor exposures.

Pure Python — no numpy / scipy / statsmodels.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_series

__all__ = [
    "RegimeFactorBetasResult",
    "regime_factor_betas_report",
]


def _ols(
    y: list[float], x: dict[str, list[float]]
) -> tuple[dict[str, float], float, int]:
    """Ordinary least squares via normal equations.

    Returns (betas, r_squared, n).
    """
    n = len(y)
    factor_names = list(x.keys())
    k = len(factor_names)
    if k == 0 or n <= k:
        return {}, 0.0, n

    # Build X matrix (design matrix): each row is [1.0, x1, x2, ...]
    # We solve (X^T X) b = X^T y

    # X^T X: (k+1) x (k+1) matrix
    p = k + 1
    xtx: list[list[float]] = [[0.0] * p for _ in range(p)]
    xty: list[float] = [0.0] * p

    for i in range(n):
        row = [1.0] + [x[name][i] for name in factor_names]
        for r in range(p):
            xty[r] += row[r] * y[i]
            for c in range(p):
                xtx[r][c] += row[r] * row[c]

    # Solve xtx * beta = xty via Gaussian elimination with partial pivoting
    # Augmented matrix
    aug = [xtx[i] + [xty[i]] for i in range(p)]
    for col in range(p):
        # Find pivot
        max_row = col
        max_val = abs(aug[col][col])
        for row in range(col + 1, p):
            if abs(aug[row][col]) > max_val:
                max_val = abs(aug[row][col])
                max_row = row
        if max_val < 1e-15:
            # Singular — skip
            continue
        if max_row != col:
            aug[col], aug[max_row] = aug[max_row], aug[col]

        # Normalize pivot row
        pivot = aug[col][col]
        for c in range(col, p + 1):
            aug[col][c] /= pivot

        # Eliminate other rows
        for row in range(p):
            if row == col:
                continue
            factor = aug[row][col]
            for c in range(col, p + 1):
                aug[row][c] -= factor * aug[col][c]

    betas_vec = [aug[r][p] for r in range(p)]
    # betas_vec[0] is the intercept, betas_vec[1:] are factor betas
    betas = {name: betas_vec[i + 1] for i, name in enumerate(factor_names)}

    # Compute R-squared
    y_mean = sum(y) / n if n > 0 else 0.0
    ss_total = sum((yi - y_mean) ** 2 for yi in y)
    ss_residual = 0.0
    for i in range(n):
        pred = betas_vec[0]  # intercept
        for j, name in enumerate(factor_names):
            pred += betas_vec[j + 1] * x[name][i]
        ss_residual += (y[i] - pred) ** 2

    r_squared = 1.0 - ss_residual / ss_total if ss_total > 1e-15 else 0.0
    return betas, r_squared, n


@dataclass(frozen=True)
class RegimeFactorBetasResult:
    """Frozen carrier for the regime-factor bas report.

    Attributes
    ----------
    per_regime: {regime: {betas: {factor: beta}, r_squared: float, n: int}}.
    beta_stability: Coefficient of variation of each factor's beta across regimes
                   (mean of per-factor CVs, or 0 when impossible to compute).
    regime_beta_spread: For each factor, max - min beta across regimes.
    """

    per_regime: dict[str, dict[str, Any]]
    beta_stability: dict[str, float]
    regime_beta_spread: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_regime": self.per_regime,
            "beta_stability": self.beta_stability,
            "regime_beta_spread": self.regime_beta_spread,
        }


def regime_factor_betas_report(
    returns: list[float],
    factor_returns: dict[str, list[float]],
    regimes: list[str],
) -> RegimeFactorBetasResult:
    """Compute per-regime factor betas and cross-regime stability.

    Parameters
    ----------
    returns: Period returns series.
    factor_returns: {factor_name: [returns]} — same length as ``returns``.
    regimes: Regime labels aligned with ``returns``.

    Returns
    -------
    RegimeFactorBetasResult with per-regime betas, stability, and spread.

    Raises
    ------
    ValueError: If inputs are invalid or misaligned.
    """
    validated_returns = validate_series(returns, name="returns", min_len=3)
    n = len(validated_returns)

    if not isinstance(factor_returns, dict) or not factor_returns:
        raise ValueError("factor_returns must be a non-empty dict")
    validated_factors: dict[str, list[float]] = {}
    for name, series in factor_returns.items():
        name_str = str(name)
        validated_factors[name_str] = validate_series(
            series, name=f"factor_returns['{name_str}']", min_len=n
        )[:n]
        if len(validated_factors[name_str]) != n:
            raise ValueError(
                f"factor_returns['{name_str}'] length {len(validated_factors[name_str])} "
                f"!= returns length {n}"
            )

    if not isinstance(regimes, list) or len(regimes) != n:
        raise ValueError(f"regimes must be a list of length {n}")

    regimes_str = [str(r) for r in regimes]
    unique_regimes = sorted(set(regimes_str))

    if len(unique_regimes) < 2:
        raise ValueError("need at least 2 distinct regime labels")

    # Per-regime OLS
    per_regime: dict[str, dict[str, Any]] = {}
    for regime in unique_regimes:
        indices = [i for i, r in enumerate(regimes_str) if r == regime]
        y_regime = [validated_returns[i] for i in indices]
        x_regime = {
            name: [validated_factors[name][i] for i in indices]
            for name in validated_factors
        }
        betas, r_squared, n_reg = _ols(y_regime, x_regime)
        per_regime[regime] = {
            "betas": betas,
            "r_squared": r_squared,
            "n": n_reg,
        }

    # Cross-regime beta stability (coefficient of variation)
    beta_stability: dict[str, float] = {}
    regime_beta_spread: dict[str, float] = {}

    for factor_name in validated_factors:
        betas_list = [
            per_regime[regime]["betas"].get(factor_name, 0.0)
            for regime in unique_regimes
        ]
        mean_b = sum(betas_list) / len(betas_list) if betas_list else 0.0
        variance = (
            sum((b - mean_b) ** 2 for b in betas_list) / len(betas_list)
            if len(betas_list) > 1
            else 0.0
        )
        std_dev = math.sqrt(variance) if variance > 0 else 0.0
        cv = std_dev / abs(mean_b) if abs(mean_b) > 1e-12 else 0.0
        beta_stability[factor_name] = float(cv)

        # Spread
        max_b = max(betas_list) if betas_list else 0.0
        min_b = min(betas_list) if betas_list else 0.0
        regime_beta_spread[factor_name] = max_b - min_b

    return RegimeFactorBetasResult(
        per_regime=per_regime,
        beta_stability=beta_stability,
        regime_beta_spread=regime_beta_spread,
    )
