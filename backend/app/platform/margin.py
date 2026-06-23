from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Callable, Protocol, runtime_checkable

__all__ = ["MarginModel", "FixedMarginModel", "LeverageGuard"]


@runtime_checkable
class MarginModel(Protocol):
    """保证金模型（参考 Nautilus MarginModel / Lean BuyingPowerModel）。"""

    def initial_margin(self, quantity: int, price: Decimal) -> Decimal: ...

    def maintenance_margin(self, quantity: int, price: Decimal, avg_cost: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class FixedMarginModel:
    """固定保证金率模型：initial = margin_rate * notional；maintenance = maint_rate * notional。"""

    margin_rate: Decimal = Decimal("0.5")
    maint_rate: Decimal = Decimal("0.25")

    def initial_margin(self, quantity: int, price: Decimal) -> Decimal:
        return self.margin_rate * Decimal(quantity) * price

    def maintenance_margin(self, quantity: int, price: Decimal, avg_cost: Decimal) -> Decimal:
        notional = Decimal(quantity) * price
        return self.maint_rate * notional


@dataclass
class LeverageGuard:
    """杠杆闸：在开/加仓前校验新总持仓名义价值 / 权益 ≤ max_leverage。

    `equity_provider` 返回当前账户权益（cash + positions market value）。
    `exposure_provider` 返回当前总名义敞口（abs(sum(qty*price))）。
    """

    max_leverage: Decimal
    equity_provider: Callable[[], Decimal]
    exposure_provider: Callable[[], Decimal]
    margin_model: MarginModel

    def projected_leverage(self, added_value: Decimal) -> Decimal:
        equity = self.equity_provider()
        if equity <= 0:
            return Decimal("0") if added_value <= 0 else Decimal("Infinity")
        current = self.exposure_provider()
        return (current + added_value) / equity

    def can_open(self, quantity: int, price: Decimal) -> bool:
        added = Decimal(quantity) * price
        return self.projected_leverage(added) <= self.max_leverage

    def available_capacity(self) -> Decimal:
        """当前还能再增加的名义敞口（使杠杆维持在 cap 内）。"""
        equity = self.equity_provider()
        if equity <= 0:
            return Decimal("0")
        cap_exposure = self.max_leverage * equity
        remaining = cap_exposure - self.exposure_provider()
        return remaining if remaining > 0 else Decimal("0")
