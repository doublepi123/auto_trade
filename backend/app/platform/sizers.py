from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.sdk import OrderIntent

__all__ = [
    "Sizer",
    "FixedFractionalSizer",
    "FullEquitySizer",
    "ATRSizer",
    "SizerRegistry",
    "get_default_registry",
    "intent_from_signal",
]


@runtime_checkable
class Sizer(Protocol):
    """仓位定尺（参考 Backtrader Sizer / Lean IPortfolioConstructionModel）。

    给定方向/价格/净值(NAV)/(可选 ATR)，返回整数股数。"""

    @property
    def name(self) -> str: ...

    def size(
        self, side: str, price: Decimal, nav: Decimal, atr: Decimal | None = None
    ) -> int: ...


@dataclass(frozen=True)
class FixedFractionalSizer:
    """按净值的固定比例下单：qty = nav * fraction / price。"""

    fraction: Decimal = Decimal("0.1")
    name: str = "fixed_fractional"

    def size(
        self, side: str, price: Decimal, nav: Decimal, atr: Decimal | None = None
    ) -> int:
        if price <= 0 or nav <= 0:
            return 0
        raw = (nav * self.fraction) / price
        return int(raw.to_integral_value())


@dataclass(frozen=True)
class FullEquitySizer:
    """全仓：qty = nav / price。"""

    name: str = "full_equity"

    def size(
        self, side: str, price: Decimal, nav: Decimal, atr: Decimal | None = None
    ) -> int:
        if price <= 0 or nav <= 0:
            return 0
        return int((nav / price).to_integral_value())


@dataclass(frozen=True)
class ATRSizer:
    """波动率风险定尺：qty = (nav * risk_fraction) / (atr * atr_multiplier)。"""

    risk_fraction: Decimal = Decimal("0.02")
    atr_multiplier: Decimal = Decimal("1")
    name: str = "atr"

    def size(
        self, side: str, price: Decimal, nav: Decimal, atr: Decimal | None = None
    ) -> int:
        if price <= 0 or nav <= 0 or atr is None or atr <= 0:
            return 0
        stop_distance = atr * self.atr_multiplier
        if stop_distance <= 0:
            return 0
        risk_amount = nav * self.risk_fraction
        return int((risk_amount / stop_distance).to_integral_value())


class SizerRegistry:
    def __init__(self) -> None:
        self._sizers: dict[str, Sizer] = {}

    def register(self, sizer: Sizer) -> None:
        if sizer.name in self._sizers:
            raise ValueError(f"Sizer '{sizer.name}' already registered")
        self._sizers[sizer.name] = sizer

    def get(self, name: str) -> Sizer:
        if name not in self._sizers:
            raise KeyError(f"Sizer '{name}' not found")
        return self._sizers[name]

    def list(self) -> list[dict[str, Any]]:
        return [{"name": s.name} for s in self._sizers.values()]


_DEFAULT_REGISTRY = SizerRegistry()
_DEFAULT_REGISTRY.register(FixedFractionalSizer())
_DEFAULT_REGISTRY.register(FullEquitySizer())
_DEFAULT_REGISTRY.register(ATRSizer())


def get_default_registry() -> SizerRegistry:
    return _DEFAULT_REGISTRY


def intent_from_signal(
    symbol: str,
    side: str,
    price: Decimal,
    sizer: Sizer,
    nav: Decimal,
    atr: Decimal | None = None,
    order_type: str = "LIMIT",
    reason: str = "sized_signal",
) -> OrderIntent | None:
    """Convert a directional signal + sizer into an OrderIntent (None if size 0)."""
    qty = sizer.size(side, price, nav, atr=atr)
    if qty <= 0:
        return None
    return OrderIntent(
        symbol=symbol,
        side=side,
        quantity=qty,
        order_type=order_type,
        limit_price=price,
        reason=reason,
    )
