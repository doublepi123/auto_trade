"""P377: Time-varying hedge ratio comparison across four methods.

Computes hedge ratios using OLS (full-sample), rolling OLS (window
regression), EWMA (exponentially-weighted covariance/variance), and
naive (beta=1). For each method, the residual volatility and hedge
effectiveness are reported.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.platform.factor_utils import mean, std, validate_pair

__all__ = ["HedgeRatioComparisonResult", "hedge_ratio_comparison_report"]

_MAX_SERIES = 5000


@dataclass(frozen=True)
class HedgeRatioComparisonResult:
    per_method: dict[str, dict[str, float]]
    best_method: str
    ratios_over_time: dict[str, list[float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "per_method": dict(self.per_method),
            "best_method": self.best_method,
            "ratios_over_time": {
                k: list(v) for k, v in self.ratios_over_time.items()
            },
        }


def _ols_beta(xs: Sequence[float], ys: Sequence[float]) -> float:
    """Compute OLS slope beta = Cov(x,y) / Var(x)."""
    n = len(xs)
    if n < 2:
        raise ValueError("need at least 2 points for regression")
    mx = mean(xs)
    my = mean(ys)
    cov = 0.0
    var_x = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        cov += dx * dy
        var_x += dx * dx
    if var_x == 0.0:
        raise ValueError("x has zero variance; hedge ratio undefined")
    return cov / var_x


def _residual_vol(ys: list[float], xs: list[float], beta: float) -> float:
    """Std deviation of y - beta * x residuals."""
    n = len(ys)
    if n < 2:
        return 0.0
    residuals = [ys[i] - beta * xs[i] for i in range(n)]
    return std(residuals, sample=True)


def _effectiveness(residual_vol: float, y_std: float) -> float:
    """1 - residual_vol / y_std — higher = better hedge."""
    if y_std == 0.0:
        return 0.0
    return 1.0 - residual_vol / y_std


def hedge_ratio_comparison_report(
    y: list[float],
    x: list[float],
    *,
    window: int = 20,
) -> HedgeRatioComparisonResult:
    """Compare four time-varying hedge ratio estimation methods.

    Args:
        y: Dependent variable (e.g. portfolio returns).
        x: Independent variable (e.g. market returns).
        window: Window size for rolling OLS and EWMA half-life approximation.

    Returns:
        HedgeRatioComparisonResult with per-method stats and time-varying ratios.

    Raises:
        ValueError: If inputs are invalid (too short, mismatched, non-finite, etc.).
    """
    ys, xs = validate_pair(y, x, x_name="x", y_name="y")
    n = len(ys)

    if isinstance(window, bool) or not isinstance(window, int):
        raise ValueError("window must be an int >= 2")
    if window < 2:
        raise ValueError("window must be at least 2")
    if window > n:
        raise ValueError(f"window ({window}) must not exceed series length ({n})")

    # --- Method 1: OLS (full-sample) ---
    ols_beta = _ols_beta(xs, ys)
    ols_res_vol = _residual_vol(ys, xs, ols_beta)
    y_std = std(ys, sample=True)
    ols_eff = _effectiveness(ols_res_vol, y_std)
    # OLS produces a single constant hedge ratio over time
    ols_ratios_over_time = [ols_beta] * n

    # --- Method 2: Rolling OLS ---
    rolling_betas: list[float] = []
    for i in range(window, n + 1):
        xw = xs[i - window : i]
        yw = ys[i - window : i]
        try:
            b = _ols_beta(xw, yw)
        except ValueError:
            b = 1.0
        rolling_betas.append(b)
    # Pad beginning with first valid beta
    full_rolling: list[float] = []
    if rolling_betas:
        full_rolling = [rolling_betas[0]] * (window - 1) + rolling_betas
    if len(full_rolling) < n:
        full_rolling = full_rolling + [full_rolling[-1]] * (n - len(full_rolling))
    # Use the latest beta as the current hedge ratio
    rolling_beta = rolling_betas[-1] if rolling_betas else 1.0
    rolling_res_vol = _residual_vol(ys, xs, rolling_beta)
    rolling_eff = _effectiveness(rolling_res_vol, y_std)

    # --- Method 3: EWMA ---
    # Exponential weighted hedge ratio using lambda = 2/(window+1)
    lam = 2.0 / (window + 1)
    ewma_cov = 0.0
    ewma_var_x = 0.0
    ewma_mx = xs[0]
    ewma_my = ys[0]
    ewma_betas: list[float] = [1.0]  # first value is naive
    for i in range(1, n):
        # Update EWMA means
        ewma_mx = lam * xs[i] + (1 - lam) * ewma_mx
        ewma_my = lam * ys[i] + (1 - lam) * ewma_my
        # Update EWMA cov and var
        dx = xs[i] - ewma_mx
        dy = ys[i] - ewma_my
        ewma_cov = lam * (dx * dy) + (1 - lam) * ewma_cov
        ewma_var_x = lam * (dx * dx) + (1 - lam) * ewma_var_x
        beta_val = ewma_cov / ewma_var_x if ewma_var_x > 0 else 1.0
        ewma_betas.append(beta_val)

    ewma_beta = ewma_betas[-1] if ewma_betas else 1.0
    ewma_res_vol = _residual_vol(ys, xs, ewma_beta)
    ewma_eff = _effectiveness(ewma_res_vol, y_std)

    # --- Method 4: Naive (beta = 1) ---
    naive_beta = 1.0
    naive_res_vol = _residual_vol(ys, xs, naive_beta)
    naive_eff = _effectiveness(naive_res_vol, y_std)

    # --- Determine best method (highest effectiveness) ---
    per_method = {
        "ols": {
            "hedge_ratio": float(ols_beta),
            "residual_vol": float(ols_res_vol),
            "effectiveness": float(ols_eff),
        },
        "rolling_ols": {
            "hedge_ratio": float(rolling_beta),
            "residual_vol": float(rolling_res_vol),
            "effectiveness": float(rolling_eff),
        },
        "ewma": {
            "hedge_ratio": float(ewma_beta),
            "residual_vol": float(ewma_res_vol),
            "effectiveness": float(ewma_eff),
        },
        "naive": {
            "hedge_ratio": float(naive_beta),
            "residual_vol": float(naive_res_vol),
            "effectiveness": float(naive_eff),
        },
    }

    best = max(per_method, key=lambda m: per_method[m]["effectiveness"])

    return HedgeRatioComparisonResult(
        per_method=per_method,
        best_method=best,
        ratios_over_time={
            "ols": ols_ratios_over_time,
            "rolling_ols": full_rolling,
            "ewma": ewma_betas,
        },
    )
