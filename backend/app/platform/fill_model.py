from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, runtime_checkable

from app.platform.events import BarEvent

__all__ = [
    "SlippageModel",
    "FixedSlippageModel",
    "VolumeShareSlippageModel",
    "CommissionModel",
    "FractionalCommissionModel",
    "FixedPerShareCommissionModel",
    "FillModel",
]


@runtime_checkable
class SlippageModel(Protocol):
    """滑点模型（参考 Nautilus FillModel）：返回应叠加到基准价上的滑点量（正数）。"""

    def slippage(self, side: str, base_price: Decimal, bar: BarEvent, quantity: int) -> Decimal: ...


@runtime_checkable
class CommissionModel(Protocol):
    """费用模型（参考 Nautilus CostModel）。"""

    def commission(self, quantity: int, price: Decimal) -> Decimal: ...


@dataclass(frozen=True)
class FixedSlippageModel:
    ticks: Decimal = Decimal("0.01")

    def slippage(self, side: str, base_price: Decimal, bar: BarEvent, quantity: int) -> Decimal:
        return self.ticks


@dataclass(frozen=True)
class VolumeShareSlippageModel:
    """成交量占比滑点：订单占 bar 成交量比例越高，滑点越大（参考 Nautilus 体积冲击模型）。"""

    price_impact: Decimal = Decimal("0.1")

    def slippage(self, side: str, base_price: Decimal, bar: BarEvent, quantity: int) -> Decimal:
        volume = int(bar.volume) if bar.volume else 0
        if volume <= 0 or quantity <= 0:
            return Decimal("0")
        share = Decimal(quantity) / Decimal(volume)
        impact_value = (share * self.price_impact) * base_price
        # quantize to cents
        return impact_value.quantize(Decimal("0.01")) if impact_value != 0 else Decimal("0")


@dataclass(frozen=True)
class FractionalCommissionModel:
    rate: Decimal = Decimal("0.0005")

    def commission(self, quantity: int, price: Decimal) -> Decimal:
        return price * Decimal(quantity) * self.rate


@dataclass(frozen=True)
class FixedPerShareCommissionModel:
    per_share: Decimal = Decimal("0.005")

    def commission(self, quantity: int, price: Decimal) -> Decimal:
        return self.per_share * Decimal(quantity)


@dataclass(frozen=True)
class FillModel:
    slippage_model: SlippageModel
    commission_model: CommissionModel
