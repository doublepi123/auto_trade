"""P356: Implied Correlation from Index & Constituent IVs.

Solve for the average implied pairwise correlation that reconciles an index's
implied volatility with the IVs of its constituents under the portfolio-variance
identity. Under equal-weight assumption:

    sigma_index² = Σ (w_i² · σ_i²) + Σ Σ_{i≠j} (w_i · w_j · ρ · σ_i · σ_j)

where w_i = 1/n and ρ is the (average) implied correlation. Solving for ρ:

    ρ = (σ_index² - Σ w_i² σ_i²) / (Σ Σ_{i≠j} w_i w_j σ_i σ_j)

Pure Python, no scipy/numpy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = [
    "ImpliedCorrelationResult",
    "implied_correlation_report",
]


def _validate_float(value: Any, label: str) -> float:
    """Validate a single finite float."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} must be a finite number")
    return number


@dataclass(frozen=True)
class ImpliedCorrelationResult:
    """Frozen carrier for implied correlation analysis.

    Attributes
    ----------
    implied_correlation: Average implied pairwise correlation ρ.
    implied_index_variance: σ_index².
    realized_weighted_variance: Σ w_i² · σ_i² (idiosyncratic floor).
    variance_decomposition: {"idiosyncratic": ..., "systematic": ...}.
    """

    implied_correlation: float
    implied_index_variance: float
    realized_weighted_variance: float
    variance_decomposition: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "implied_correlation": self.implied_correlation,
            "implied_index_variance": self.implied_index_variance,
            "realized_weighted_variance": self.realized_weighted_variance,
            "variance_decomposition": self.variance_decomposition,
        }


def implied_correlation_report(
    index_iv: float,
    stock_ivs: dict[str, float],
    *,
    equal_weighted: bool = True,
) -> ImpliedCorrelationResult:
    """Compute the average implied correlation from index IV and stock IVs.

    Parameters
    ----------
    index_iv: Implied volatility of the index (e.g. 0.20 = 20%).
    stock_ivs: {ticker: implied_volatility} for constituents.
    equal_weighted: If True, assume equal weights w_i = 1/n.

    Returns a frozen result with implied correlation and variance decomposition.

    Raises ValueError/TypeError on invalid input.
    """
    index_iv_validated = _validate_float(index_iv, "index_iv")
    if index_iv_validated <= 0:
        raise ValueError("index_iv must be > 0")

    if not isinstance(stock_ivs, dict) or not stock_ivs:
        raise ValueError("stock_ivs must be a non-empty dict")
    if len(stock_ivs) < 2:
        raise ValueError("stock_ivs must contain at least 2 stocks")

    validated_ivs: list[float] = []
    for name, iv in stock_ivs.items():
        iv_val = _validate_float(iv, f"stock_ivs['{name}']")
        if iv_val <= 0:
            raise ValueError(f"stock_ivs['{name}'] must be > 0")
        validated_ivs.append(iv_val)

    n = len(validated_ivs)
    # Equal weight: w_i = 1/n
    w = 1.0 / n

    # Index variance.
    index_var = index_iv_validated ** 2

    # Idiosyncratic component: Σ w_i² · σ_i².
    idiosyncratic_var = sum(w * w * (iv ** 2) for iv in validated_ivs)

    # Systematic component denominator: Σ Σ_{i≠j} w_i w_j σ_i σ_j.
    cross_sum = 0.0
    for i in range(n):
        for j in range(n):
            if i != j:
                cross_sum += w * w * validated_ivs[i] * validated_ivs[j]

    if cross_sum <= 0:
        # All IVs are zero? Shouldn't happen given the >0 checks above.
        # But handle defensively.
        rho_implied = 0.0
        systematic_var = 0.0
    else:
        rho_implied = (index_var - idiosyncratic_var) / cross_sum
        systematic_var = rho_implied * cross_sum

    # Clamp to [0, 1] for noisy inputs.
    rho_implied = max(0.0, min(1.0, rho_implied))

    return ImpliedCorrelationResult(
        implied_correlation=rho_implied,
        implied_index_variance=index_var,
        realized_weighted_variance=idiosyncratic_var,
        variance_decomposition={
            "idiosyncratic": idiosyncratic_var,
            "systematic": systematic_var,
        },
    )
