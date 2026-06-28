"""P368: Liquidity-adjusted Information Ratio.

Computes traditional IR, then subtracts liquidity costs (spread + market impact)
to produce a liquidity-adjusted IR. The liquidity drag is the difference.
Pure Python, no new deps.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class LiquidityAdjustedIRResult:
    traditional_ir: float
    liquidity_adjusted_ir: float
    liquidity_drag: float
    cost_decomposition: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "traditional_ir": self.traditional_ir,
            "liquidity_adjusted_ir": self.liquidity_adjusted_ir,
            "liquidity_drag": self.liquidity_drag,
            "cost_decomposition": self.cost_decomposition,
        }


def _validate_series(
    returns: list[float], volumes: list[float]
) -> tuple[list[float], list[float]]:
    """Validate returns and volumes: non-empty, equal-length, all finite."""
    if not isinstance(returns, list) or not returns:
        raise ValueError("returns must be a non-empty list of finite numbers")
    if not isinstance(volumes, list) or not volumes:
        raise ValueError("volumes must be a non-empty list of finite numbers")
    if len(returns) != len(volumes):
        raise ValueError("returns and volumes must have equal length")

    validated_returns: list[float] = []
    for v in returns:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("returns entries must be finite numbers")
        f = float(v)
        if not math.isfinite(f):
            raise ValueError("returns entries must be finite numbers")
        validated_returns.append(f)

    validated_volumes: list[float] = []
    for v in volumes:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("volumes entries must be finite numbers")
        f = float(v)
        if not math.isfinite(f) or f <= 0:
            raise ValueError("volumes entries must be positive finite numbers")
        validated_volumes.append(f)

    return validated_returns, validated_volumes


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Sample standard deviation."""
    n = len(xs)
    if n < 2:
        return 0.0
    m = _mean(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def liquidity_adjusted_ir_report(
    returns: list[float],
    volumes: list[float],
    *,
    spread_bps: float = 5.0,
    turnover: float = 0.1,
    periods_per_year: int = 252,
) -> LiquidityAdjustedIRResult:
    """Compute traditional and liquidity-adjusted Information Ratio.

    Parameters
    ----------
    returns:
        List of period returns.
    volumes:
        List of corresponding trade volumes.
    spread_bps:
        Bid-ask spread in basis points (default 5.0).
    turnover:
        Annual turnover rate (default 0.1).
    periods_per_year:
        Number of periods per year for annualization (default 252).

    Returns
    -------
    LiquidityAdjustedIRResult with traditional_ir, liquidity_adjusted_ir,
    liquidity_drag, and cost_decomposition.
    """
    validated_returns, validated_volumes = _validate_series(returns, volumes)

    mu = _mean(validated_returns)
    sigma = _std(validated_returns)

    # Traditional IR (annualized)
    if sigma == 0.0:
        traditional_ir = 0.0
    else:
        traditional_ir = (mu / sigma) * math.sqrt(periods_per_year)

    # Spread cost: spread_bps / 10000 * turnover * periods_per_year
    spread_cost_annual = (spread_bps / 10000.0) * turnover * periods_per_year

    # Impact cost: simplified Amihud
    # impact = 0.5 * sqrt(turnover * mean(|r|/volume)) * 100
    impact_terms: list[float] = []
    for r, v in zip(validated_returns, validated_volumes):
        if v > 0:
            impact_terms.append(abs(r) / v)
    if impact_terms:
        avg_impact = _mean(impact_terms)
        impact_cost_annual = 0.5 * math.sqrt(turnover * avg_impact) * 100.0
    else:
        impact_cost_annual = 0.0

    # Per-period cost
    spread_cost_per_period = spread_cost_annual / periods_per_year
    impact_cost_per_period = impact_cost_annual / periods_per_year

    # Adjusted returns
    adjusted_returns = [
        r - spread_cost_per_period - impact_cost_per_period
        for r in validated_returns
    ]

    adj_mu = _mean(adjusted_returns)
    adj_sigma = _std(adjusted_returns)

    if adj_sigma == 0.0:
        liquidity_adjusted_ir = 0.0
    else:
        liquidity_adjusted_ir = (adj_mu / adj_sigma) * math.sqrt(periods_per_year)

    liquidity_drag = traditional_ir - liquidity_adjusted_ir

    return LiquidityAdjustedIRResult(
        traditional_ir=traditional_ir,
        liquidity_adjusted_ir=liquidity_adjusted_ir,
        liquidity_drag=liquidity_drag,
        cost_decomposition={
            "spread": spread_cost_annual,
            "impact": impact_cost_annual,
        },
    )
