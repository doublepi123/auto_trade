"""P340: Systemic risk measurement — ΔCoVaR + MES.

ΔCoVaR: quantile regression approximation: difference between the target's
conditional VaR when the market is in its lower α tail vs its median state.
MES (Marginal Expected Shortfall): average target return during the market's
worst 5% periods (absolute value).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, validate_series


@dataclass(frozen=True)
class SystemicRiskResult:
    delta_covar: float
    mes: float
    covar_target_down: float
    covar_target_median: float
    systemic_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_covar": self.delta_covar,
            "mes": self.mes,
            "covar_target_down": self.covar_target_down,
            "covar_target_median": self.covar_target_median,
            "systemic_score": self.systemic_score,
        }


def _quantile(values: list[float], q: float) -> float:
    """Return the q-quantile of a sorted list."""
    if not values:
        return 0.0
    sorted_vals = sorted(values)
    n = len(sorted_vals)
    idx = q * (n - 1)
    lo = int(idx)
    hi = min(lo + 1, n - 1)
    frac = idx - lo
    return sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac


def _conditional_var(returns: list[float], condition_returns: list[float],
                     threshold_quantile: float) -> float:
    """VaR of returns when condition_returns is below its threshold_quantile."""
    threshold = _quantile(condition_returns, threshold_quantile)
    tail = [r for r, c in zip(returns, condition_returns) if c <= threshold]
    if not tail:
        tail = returns[:]
    return -_quantile(tail, threshold_quantile)  # VaR of returns in tail (lower quantile)


def systemic_risk_report(
    target_returns: list[float],
    market_returns: list[float],
    *,
    confidence: float = 0.95,
) -> SystemicRiskResult:
    """Compute ΔCoVaR and MES for a target vs market return series.

    ΔCoVaR = CoVaR(down) - CoVaR(median)
    where CoVaR(down) is target VaR when market is in lower α tail,
    and CoVaR(median) is target VaR when market is around median.
    MES = |mean of target returns during market's worst 5% periods|.
    """
    target = validate_series(target_returns, name="target_returns", min_len=5)
    market = validate_series(market_returns, name="market_returns", min_len=5)
    if len(target) != len(market):
        raise ValueError("target_returns and market_returns must have the same length")

    alpha = 1.0 - confidence  # lower tail

    # CoVaR: target VaR when market is in lower α tail
    market_threshold = _quantile(market, alpha)
    target_in_tail = [r for r, c in zip(target, market) if c <= market_threshold]
    covar_down = -_quantile(target_in_tail, alpha) if target_in_tail else 0.0

    # CoVaR at median: target VaR when market is around median
    median = _quantile(market, 0.5)
    target_in_median = [r for r, c in zip(target, market)
                        if abs(c - median) <= 0.1 * (max(market) - min(market) + 1e-12)]
    if len(target_in_median) < 2:
        target_in_median = target
    covar_median = -_quantile(target_in_median, alpha)

    delta_covar = covar_down - covar_median
    # Ensure delta_covar is non-negative for positively correlated assets
    delta_covar = max(0.0, delta_covar)

    # MES: average target return during market's worst 5%
    market_tail_threshold = _quantile(market, 0.05)
    mes_returns = [r for r, c in zip(target, market) if c <= market_tail_threshold]
    mes = abs(mean(mes_returns)) if mes_returns else 0.0

    # Systemic score: composite of ΔCoVaR and MES, normalized
    systemic_score = delta_covar + mes

    return SystemicRiskResult(
        delta_covar=delta_covar,
        mes=mes,
        covar_target_down=covar_down,
        covar_target_median=covar_median,
        systemic_score=systemic_score,
    )
