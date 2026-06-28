"""P364: Capacity scaling — AUM impact on Sharpe ratio.

Pure-Python capacity estimator: scales AUM as multiples of ADV, estimates
market-impact cost via a square-root participation model, and computes
the resulting net Sharpe ratio degradation curve.

Public surface
--------------

* **capacity_scaling_report(returns, adv, turnover, aum_multipliers,
  periods_per_year)** → frozen :class:`CapacityScalingResult` with
  ``scaling_curve``, ``capacity_limit``, and ``sharpe_decay_rate``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

from app.platform.factor_utils import mean, std, validate_series

__all__ = ["CapacityScalingResult", "capacity_scaling_report"]


@dataclass(frozen=True)
class CapacityScalingResult:
    scaling_curve: list[dict[str, Any]]
    capacity_limit: float
    sharpe_decay_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "scaling_curve": [dict(s) for s in self.scaling_curve],
            "capacity_limit": self.capacity_limit,
            "sharpe_decay_rate": self.sharpe_decay_rate,
        }


def capacity_scaling_report(
    returns: list[float],
    adv: float,
    turnover: float,
    *,
    aum_multipliers: list[float] | None = None,
    periods_per_year: int = 252,
) -> CapacityScalingResult:
    validated = validate_series(returns, name="returns", min_len=2)

    if isinstance(adv, bool) or not isinstance(adv, (int, float)):
        raise ValueError("adv must be a finite positive number")
    adv_f = float(adv)
    if not math.isfinite(adv_f) or adv_f <= 0:
        raise ValueError("adv must be a finite positive number")

    if isinstance(turnover, bool) or not isinstance(turnover, (int, float)):
        raise ValueError("turnover must be a finite positive number")
    turnover_f = float(turnover)
    if not math.isfinite(turnover_f) or turnover_f <= 0:
        raise ValueError("turnover must be a finite positive number")

    if aum_multipliers is None:
        aum_multipliers = [0.01, 0.05, 0.1, 0.2, 0.5, 1.0]
    if not isinstance(aum_multipliers, list) or not aum_multipliers:
        raise ValueError("aum_multipliers must be a non-empty list of finite positive numbers")
    multipliers: list[float] = []
    for v in aum_multipliers:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError("aum_multipliers entries must be finite positive numbers")
        fv = float(v)
        if not math.isfinite(fv) or fv <= 0:
            raise ValueError("aum_multipliers entries must be finite positive numbers")
        multipliers.append(fv)

    if isinstance(periods_per_year, bool) or not isinstance(periods_per_year, int):
        raise ValueError("periods_per_year must be an int >= 1")
    if periods_per_year < 1:
        raise ValueError("periods_per_year must be an int >= 1")

    mu = mean(validated)
    sigma = std(validated)
    annualization = math.sqrt(periods_per_year)

    gross_sharpe = 0.0
    if sigma > 0:
        gross_sharpe = mu / sigma * annualization
    else:
        gross_sharpe = 0.0

    scaling_curve: list[dict[str, Any]] = []
    for mult in multipliers:
        aum = mult * adv_f
        participation = aum * turnover_f / adv_f
        if participation <= 0:
            impact_cost = 0.0
        else:
            impact_cost = 0.5 * math.sqrt(participation) * turnover_f
        net_return = mu - impact_cost
        if sigma > 0:
            net_sharpe = net_return / sigma * annualization
        else:
            net_sharpe = 0.0
        scaling_curve.append({
            "aum": round(aum, 2),
            "gross_sharpe": round(gross_sharpe, 6),
            "impact_cost": round(impact_cost, 8),
            "net_sharpe": round(net_sharpe, 6),
        })

    # capacity_limit = AUM where net_sharpe drops to 1.0 (linear interpolation)
    capacity_limit = 0.0
    cap_found = False
    for i in range(len(scaling_curve) - 1):
        s0 = scaling_curve[i]
        s1 = scaling_curve[i + 1]
        if s0["net_sharpe"] >= 1.0 >= s1["net_sharpe"]:
            # linear interpolation
            if abs(s1["net_sharpe"] - s0["net_sharpe"]) > 1e-12:
                frac = (1.0 - s0["net_sharpe"]) / (s1["net_sharpe"] - s0["net_sharpe"])
            else:
                frac = 0.0
            capacity_limit = s0["aum"] + frac * (s1["aum"] - s0["aum"])
            cap_found = True
            break
    if not cap_found:
        # net_sharpe always >= 1.0 → use max AUM
        if scaling_curve and scaling_curve[-1]["net_sharpe"] >= 1.0:
            capacity_limit = scaling_curve[-1]["aum"]
        # net_sharpe always < 1.0 → use min AUM
        elif scaling_curve and scaling_curve[0]["net_sharpe"] < 1.0:
            capacity_limit = scaling_curve[0]["aum"]

    # sharpe_decay_rate = slope of (gross - net) sharpe vs AUM
    sharpe_decay_rate = 0.0
    if len(scaling_curve) >= 2:
        first = scaling_curve[0]
        last = scaling_curve[-1]
        aum_range = last["aum"] - first["aum"]
        if aum_range > 0:
            decay_range = first["net_sharpe"] - last["net_sharpe"]
            sharpe_decay_rate = decay_range / aum_range
        else:
            sharpe_decay_rate = 0.0

    return CapacityScalingResult(
        scaling_curve=scaling_curve,
        capacity_limit=round(capacity_limit, 2),
        sharpe_decay_rate=round(sharpe_decay_rate, 12),
    )
