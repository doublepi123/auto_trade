"""P291: pre-trade cost estimate."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class PretradeCostResult:
    notional: float
    participation_rate: float
    spread_cost_bps: float
    impact_cost_bps: float
    timing_risk_bps: float
    total_cost_bps: float
    total_cost: float
    efficient_frontier: list[dict[str, float]]

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def pretrade_cost_report(*, order_qty: float, adv: float, price: float, spread_bps: float = 0.0, volatility: float = 0.0, impact_coefficient: float = 0.1) -> PretradeCostResult:
    qty = _positive(order_qty, "order_qty")
    daily_volume = _positive(adv, "adv")
    px = _positive(price, "price")
    spread = _non_negative(spread_bps, "spread_bps")
    vol = _non_negative(volatility, "volatility")
    coeff = _non_negative(impact_coefficient, "impact_coefficient")
    return _estimate(qty, daily_volume, px, spread, vol, coeff)


def _estimate(qty: float, adv: float, price: float, spread: float, vol: float, coeff: float) -> PretradeCostResult:
    participation = qty / adv
    spread_cost = spread / 2.0
    impact = coeff * vol * math.sqrt(participation) * 10000.0
    timing = vol * participation * 100.0
    total_bps = spread_cost + impact + timing
    notional = qty * price
    frontier = []
    for part in [0.01, 0.05, 0.10, 0.20]:
        f_qty = adv * part
        f_impact = coeff * vol * math.sqrt(part) * 10000.0
        f_timing = vol * part * 100.0
        frontier.append({"participation_rate": part, "order_qty": f_qty, "total_cost_bps": spread_cost + f_impact + f_timing})
    return PretradeCostResult(notional, participation, spread_cost, impact, timing, total_bps, notional * total_bps / 10000.0, frontier)


def _positive(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def _non_negative(value: Any, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise ValueError(f"{name} must be non-negative")
    return number


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


__all__ = ["PretradeCostResult", "pretrade_cost_report"]
