"""P312: Liquidity-Adjusted Returns — Amihud & Roll illiquidity adjustments.

Adjust raw returns for illiquidity using two classical microstructure models:

* **Amihud** (2002): illiquidity = mean(|r_i| / volume_i). The penalty
  subtracts ``illiquidity_metric × volume_i`` from each return.
* **Roll** (1984): spread = 2 × √(max(0, −cov(Δp_t, Δp_{t−1}))).
  Adjusted returns = raw returns − spread_cost, where spread_cost is the
  estimated effective half-spread.

Deterministic, pure Python. Reference: Amihud (2002) J. Financial Markets,
Roll (1984) J. Finance.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import validate_pair

__all__ = [
    "LiquidityAdjustedReturnsResult",
    "liquidity_adjusted_returns_report",
]


@dataclass(frozen=True)
class LiquidityAdjustedReturnsResult:
    raw_returns: list[float]
    adjusted_returns: list[float]
    illiquidity_metric: float
    method: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "raw_returns": self.raw_returns,
            "adjusted_returns": self.adjusted_returns,
            "illiquidity_metric": self.illiquidity_metric,
            "method": self.method,
        }


def _amihud_adjust(returns: list[float], volumes: list[float]) -> tuple[list[float], float]:
    """Amihud illiquidity: mean(|r_i| / v_i). Penalty = illiquidity metric."""
    ratios = [abs(r) / v for r, v in zip(returns, volumes)]
    illiquidity = sum(ratios) / len(ratios) if ratios else 0.0
    # Adjusted = raw return minus the illiquidity spread cost
    adjusted = [r - illiquidity for r in returns]
    return adjusted, illiquidity


def _roll_adjust(returns: list[float], volumes: list[float]) -> tuple[list[float], float]:
    """Roll spread: 2 × √(max(0, −cov(Δp_t, Δp_{t−1})))."""
    if len(returns) < 3:
        # Need at least 3 points for two price changes
        return list(returns), 0.0

    n = len(returns)
    deltas = [returns[i] - returns[i - 1] for i in range(1, n)]
    if len(deltas) < 2:
        return list(returns), 0.0

    mean_delta = sum(deltas) / len(deltas)
    # Cov(Δp_t, Δp_{t-1})
    cov_sum = 0.0
    for i in range(len(deltas) - 1):
        cov_sum += (deltas[i] - mean_delta) * (deltas[i + 1] - mean_delta)
    cov = cov_sum / len(deltas) if deltas else 0.0

    spread = 2.0 * math.sqrt(max(0.0, -cov))
    # Effective half-spread cost proportional to the spread estimate
    # Adjusted = raw − spread × normalized volume factor
    avg_vol = sum(volumes) / len(volumes) if volumes else 1.0
    adjusted = [r - spread * (avg_vol / v) * 0.5 for r, v in zip(returns, volumes)]
    return adjusted, spread


def liquidity_adjusted_returns_report(
    returns: list[float],
    volumes: list[float],
    *,
    method: str = "amihud",
) -> LiquidityAdjustedReturnsResult:
    """Adjust *returns* for illiquidity using the specified *method*.

    *method* must be ``"amihud"`` or ``"roll"``. Returns and volumes are
    validated as equal-length series via ``validate_pair``. Zero volumes
    are rejected.
    """
    if method not in ("amihud", "roll"):
        raise ValueError("method must be 'amihud' or 'roll'")

    rs, vs = validate_pair(returns, volumes, x_name="returns", y_name="volumes")

    for v in vs:
        if v <= 0:
            raise ValueError("volumes must be positive")

    if method == "amihud":
        adjusted, illiquidity = _amihud_adjust(rs, vs)
    else:
        adjusted, illiquidity = _roll_adjust(rs, vs)

    return LiquidityAdjustedReturnsResult(
        raw_returns=rs,
        adjusted_returns=adjusted,
        illiquidity_metric=illiquidity,
        method=method,
    )
