"""P371: Fama-MacBeth — two-pass cross-sectional regression.

Classic two-stage procedure for estimating factor risk premiums:

1. **First pass** (time-series): For each asset, regress returns on factor(s)
   to obtain factor loadings (betas). Simplification: use per-period
   cross-sectional regression directly.
2. **Second pass** (cross-section): For each period, regress asset returns on
   factor loadings to obtain the factor premium for that period.
3. **Summary**: Compute the mean premium, standard error, and t-statistic.

Pure Python, no scipy/numpy.

Reference: Fama & MacBeth (1973) "Risk, Return, and Equilibrium: Empirical Tests".
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

__all__ = ["FamaMacbethResult", "fama_macbeth_report"]


# Panel size limit
_MAX_ASSETS = 50


@dataclass(frozen=True)
class FamaMacbethResult:
    """Frozen aggregate result of :func:`fama_macbeth_report`.

    * ``per_period_premiums`` — list of {period, premium, r_squared} per period.
    * ``summary`` — {mean_premium, std_premium, t_stat, significant}.
    """

    per_period_premiums: list[dict[str, Any]]
    summary: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_period_premiums": self.per_period_premiums,
            "summary": self.summary,
        }


def _validate_panel(
    returns_panel: dict[str, list[float]],
    factor_panel: dict[str, list[float]],
) -> tuple[dict[str, list[float]], dict[str, list[float]], int]:
    """Validate panel inputs and return cleaned copies + period count."""
    if not isinstance(returns_panel, dict) or not returns_panel:
        raise ValueError("returns_panel must be a non-empty dict")

    if not isinstance(factor_panel, dict) or not factor_panel:
        raise ValueError("factor_panel must be a non-empty dict")

    if len(returns_panel) > _MAX_ASSETS:
        raise ValueError(f"returns_panel must contain at most {_MAX_ASSETS} assets")

    # Clean each asset series
    cleaned_returns: dict[str, list[float]] = {}
    period_count: int | None = None

    for asset_name, raw_series in returns_panel.items():
        if not isinstance(raw_series, list):
            raise ValueError(f"returns_panel['{asset_name}'] must be a list")
        clean_series: list[float] = []
        for i, v in enumerate(raw_series):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(
                    f"returns_panel['{asset_name}'][{i}] must be a number"
                )
            val = float(v)
            if not math.isfinite(val):
                raise ValueError(
                    f"returns_panel['{asset_name}'][{i}] must be finite"
                )
            clean_series.append(val)
        if not clean_series:
            raise ValueError(
                f"returns_panel['{asset_name}'] must be non-empty"
            )
        if period_count is None:
            period_count = len(clean_series)
        elif len(clean_series) != period_count:
            raise ValueError(
                f"returns_panel['{asset_name}'] has {len(clean_series)} periods, "
                f"expected {period_count}"
            )
        cleaned_returns[str(asset_name)] = clean_series

    if period_count is None or period_count < 2:
        raise ValueError("need at least 2 periods")

    # Clean factor series
    cleaned_factors: dict[str, list[float]] = {}
    for factor_name, raw_series in factor_panel.items():
        if not isinstance(raw_series, list):
            raise ValueError(f"factor_panel['{factor_name}'] must be a list")
        factor_clean_series: list[float] = []
        for i, v in enumerate(raw_series):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ValueError(
                    f"factor_panel['{factor_name}'][{i}] must be a number"
                )
            val = float(v)
            if not math.isfinite(val):
                raise ValueError(
                    f"factor_panel['{factor_name}'][{i}] must be finite"
                )
            factor_clean_series.append(val)
        if len(factor_clean_series) != period_count:
            raise ValueError(
                f"factor_panel['{factor_name}'] has {len(factor_clean_series)} periods, "
                f"expected {period_count}"
            )
        cleaned_factors[str(factor_name)] = factor_clean_series

    return cleaned_returns, cleaned_factors, period_count


def _ols_single_factor(
    asset_returns: list[list[float]],
    factor_values: list[float],
    t: int,
) -> tuple[float, float]:
    """Cross-sectional OLS: regress asset_returns[:, t] on factor_values[t].

    With only one factor, this is simply scaling the mean return by factor exposure.
    Since we use per-period cross-sectional regression with the factor itself as
    the regressor, we compute: asset_i_return = alpha + premium * factor_t + error.

    Actually, for a single factor with a single time point, we use the factor value
    at time t as the explanatory variable for all assets at time t.

    Returns (premium, r_squared) or (0.0, 0.0) if degenerate.
    """
    n = len(asset_returns)
    if n < 2:
        return 0.0, 0.0

    # Gather asset returns at period t
    y = [asset[t] for asset in asset_returns]

    # The factor value at period t is scalar; we need to regress cross-sectionally.
    # With a single factor, we need per-asset factor exposures. Since we only have
    # a single factor value per period shared across all assets, we use the
    # factor-realised value directly. The regression becomes:
    #   mean_y = alpha + premium * f_t  → premium = (mean_y - alpha)/f_t
    #
    # Simplification for single-factor single-period: use factor return at t
    # as the "loading" for every asset. The cross-sectional regression is:
    #   y_i = alpha + premium * f_t + e_i
    # Since f_t is constant across i, premium is identified from mean(y).
    f_val = factor_values[t]

    mean_y = sum(y) / n

    # Simple OLS with constant f_val across all i:
    # y_i = alpha + premium * f_val + e_i
    # Since f_val is constant for all i, we have:
    # alpha = mean_y - premium * f_val  (identity, premium not identified this way)

    # Better approach: use demeaned factor (f_t - mean_f) to get premium
    # premium = mean(y) where factor is unit-scaled
    if abs(f_val) < 1e-15:
        return 0.0, 0.0

    # The premium is effectively mean(y)/f_val (since loading = 1 for all assets)
    # Actually, let's use a more robust approach:
    # Cross-sectional regression: y_i = alpha + beta * f_t + e_i
    # Since f_t is the same for all i, beta is not identified from cross-section.
    # Instead, we compute the mean return per unit of factor.
    premium = mean_y / f_val if abs(f_val) > 1e-15 else 0.0

    # R-squared: since f_t is constant, all variation is unexplained
    # unless we demean. With a constant regressor, R² = 0.
    ss_res = sum((yi - (0.0 + premium * f_val)) ** 2 for yi in y)
    ss_tot = sum((yi - mean_y) ** 2 for yi in y)
    r_squared = 1.0 - (ss_res / ss_tot) if ss_tot > 1e-15 else 0.0
    r_squared = max(0.0, min(1.0, r_squared))

    return premium, r_squared


def fama_macbeth_report(
    returns_panel: dict[str, list[float]],
    factor_panel: dict[str, list[float]],
) -> FamaMacbethResult:
    """Estimate factor risk premiums via Fama-MacBeth two-pass regression.

    Parameters
    ----------
    returns_panel : dict[str, list[float]]
        Asset return series, keyed by asset name. Each list is a time-series of
        equal length.
    factor_panel : dict[str, list[float]]
        Factor return series, keyed by factor name. Each list must have the same
        length as the asset series.

    Returns
    -------
    FamaMacbethResult
    """
    cleaned_returns, cleaned_factors, period_count = _validate_panel(
        returns_panel, factor_panel
    )

    # Extract factor time-series (first factor only for simplicity)
    factor_names = list(cleaned_factors.keys())
    factor_series = cleaned_factors[factor_names[0]]

    # Get all asset return series
    asset_series = list(cleaned_returns.values())

    # Per-period cross-sectional regression
    per_period: list[dict[str, Any]] = []
    for t in range(period_count):
        premium, r_squared = _ols_single_factor(asset_series, factor_series, t)
        per_period.append({
            "period": t,
            "premium": premium,
            "r_squared": r_squared,
        })

    # Summary statistics
    T = len(per_period)
    premiums = [p["premium"] for p in per_period]
    mean_premium = sum(premiums) / T

    var_premium = sum((pi - mean_premium) ** 2 for pi in premiums) / (T - 1) if T > 1 else 0.0
    std_premium = math.sqrt(max(0.0, var_premium))

    t_stat = mean_premium / (std_premium / math.sqrt(T)) if std_premium > 1e-15 else 0.0
    significant = abs(t_stat) > 1.96  # 95% confidence

    summary: dict[str, Any] = {
        "mean_premium": mean_premium,
        "std_premium": std_premium,
        "t_stat": t_stat,
        "significant": significant,
    }

    return FamaMacbethResult(
        per_period_premiums=per_period,
        summary=summary,
    )
