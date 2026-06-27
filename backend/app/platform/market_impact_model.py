"""P304: market impact model diagnostics."""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any


@dataclass(frozen=True)
class MarketImpactResult:
    notional: float
    participation_rate: float
    temporary_impact_bps: float
    permanent_impact_bps: float
    total_impact_bps: float

    def to_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def market_impact_model_report(*, order_qty: float, adv: float, volatility: float, participation: float = 0.1, model: str = "square_root", permanent_fraction: float = 0.5, price: float = 1.0) -> MarketImpactResult:
    qty = _positive(order_qty, "order_qty")
    _positive(adv, "adv")
    vol = _non_negative(volatility, "volatility")
    part = _non_negative(participation, "participation")
    px = _positive(price, "price")
    if part > 1:
        raise ValueError("participation must be in [0, 1]")
    if model not in {"square_root", "linear"}:
        raise ValueError("model must be 'square_root' or 'linear'")
    if permanent_fraction < 0 or permanent_fraction > 1:
        raise ValueError("permanent_fraction must be in [0, 1]")
    participation_rate = part
    impact = vol * (math.sqrt(participation_rate) if model == "square_root" else participation_rate) * 10000.0 * 0.5
    temporary = impact * (1 - permanent_fraction)
    permanent = impact * permanent_fraction
    return MarketImpactResult(qty * px, participation_rate, temporary, permanent, temporary + permanent)


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


__all__ = ["MarketImpactResult", "market_impact_model_report"]
