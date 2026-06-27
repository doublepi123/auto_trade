"""P314: Tail hedge cost – Hill-estimator based put-protection cost.

Estimates the annualized cost of tail hedging using a Hill estimator
for the tail index and empirical CVaR beyond a confidence level.

Reference: Hill (1975) tail index estimator; CVaR tail hedge costing.
Pure-Python — no scipy, no NumPy, no RNG.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series

__all__ = ["TailHedgeCostResult", "tail_hedge_cost_report"]


@dataclass(frozen=True)
class TailHedgeCostResult:
    tail_index: float
    var: float
    cvar: float
    hedge_cost_annual: float
    confidence: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "tail_index": self.tail_index,
            "var": self.var,
            "cvar": self.cvar,
            "hedge_cost_annual": self.hedge_cost_annual,
            "confidence": self.confidence,
        }


def _hill_estimator(values: list[float], k: int | None = None) -> float:
    """Hill's tail-index estimator on loss magnitudes.

    ``values`` should be *loss* magnitudes (negative returns flipped to positive).
    Returns α (tail index).
    """
    if not values:
        return 0.0
    x = sorted(v for v in values if v > 0)
    n = len(x)
    if n < 4:
        return 0.0
    if k is None:
        k = max(2, int(math.sqrt(n)))
    k = min(k, n - 1)
    threshold = x[n - k - 1]
    if threshold <= 0:
        return 0.0
    log_sum = 0.0
    for i in range(n - k, n):
        if x[i] <= 0:
            continue
        log_sum += math.log(x[i] / threshold)
    if log_sum <= 0:
        return 0.0
    return k / log_sum


def tail_hedge_cost_report(
    returns: list[float],
    *,
    confidence: float = 0.95,
) -> TailHedgeCostResult:
    """Estimate annualized tail hedge cost from a return series.

    Uses Hill estimator to derive tail index, then computes empirical
    VaR and CVaR at the given confidence. Hedge cost is estimated as
    the annualized CVaR beyond confidence × frequency adjustment.

    Args:
        returns: Return series (e.g., daily log returns).
        confidence: VaR/CVaR confidence level in (0, 1).

    Returns:
        TailHedgeCostResult with tail_index, var, cvar, hedge_cost_annual.
    """
    rets = validate_series(returns, name="returns", min_len=4)

    if not isinstance(confidence, (int, float)):
        raise ValueError("confidence must be a float")
    if isinstance(confidence, bool):
        raise ValueError("confidence must be a float")
    c = float(confidence)
    if not 0.0 < c < 1.0:
        raise ValueError("confidence must be in (0, 1)")

    # Loss series: negative returns flipped to positive
    losses = sorted([-r for r in rets if r < 0])
    if not losses:
        # No losses at all => no tail risk
        return TailHedgeCostResult(
            tail_index=0.0,
            var=0.0,
            cvar=0.0,
            hedge_cost_annual=0.0,
            confidence=c,
        )

    # Hill estimator on loss magnitudes
    tail_index = _hill_estimator(losses)

    # Empirical VaR: the (1 - c) quantile of the return distribution
    sorted_rets = sorted(rets)
    n = len(sorted_rets)
    cutoff = max(0, int(math.floor((1.0 - c) * n)) - 1)
    cutoff = min(n - 1, cutoff)
    worst_return = sorted_rets[cutoff]
    # VaR is the loss magnitude; if the quantile return is non-negative,
    # there is no loss at this confidence level
    var_value = 0.0 if worst_return >= 0 else -worst_return

    # Empirical CVaR: average of losses beyond VaR
    tail_losses = [-r for r in sorted_rets if -r >= var_value]
    if not tail_losses:
        # If no tail losses found, use the worst ones
        tail_losses = [-r for r in sorted_rets[: max(1, int((1.0 - c) * n))]]
    cvar_value = mean(tail_losses) if tail_losses else var_value

    # Hedge cost: CVaR beyond confidence, annualized
    # Assume daily returns => multiply by sqrt(252) for annual scale
    # The cost is (CVaR - VaR) * frequency ≈ the expected loss beyond VaR
    annual_factor = math.sqrt(252)
    hedge_cost = max(0.0, cvar_value) * annual_factor

    return TailHedgeCostResult(
        tail_index=tail_index,
        var=var_value,
        cvar=cvar_value,
        hedge_cost_annual=hedge_cost,
        confidence=c,
    )
