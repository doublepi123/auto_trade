from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.sdk import OrderIntent

__all__ = ["PortfolioConstructionModel", "EqualWeightModel", "RiskParityModel", "weights_to_intents"]


@runtime_checkable
class PortfolioConstructionModel(Protocol):
    """组合构造模型（参考 Lean IPortfolioConstructionModel）：由信号/得分产出目标权重。"""

    @property
    def name(self) -> str: ...

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]: ...


@dataclass(frozen=True)
class EqualWeightModel:
    """等权：在非零信号标的上均分权重。"""

    name: str = "equal_weight"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        w = Decimal("1") / Decimal(len(active))
        return {s: w for s in active}


@dataclass(frozen=True)
class RiskParityModel:
    """风险平价（反波动加权）：权重 ∝ 1/vol，归一化求和为 1。"""

    name: str = "risk_parity"

    def target_weights(
        self,
        signals: dict[str, Decimal],
        *,
        volatilities: dict[str, Decimal] | None = None,
    ) -> dict[str, Decimal]:
        active = [s for s, v in signals.items() if v != 0]
        if not active:
            return {}
        vols = volatilities or {}
        inv: dict[str, Decimal] = {}
        for s in active:
            vol = vols.get(s, Decimal("0"))
            inv[s] = (Decimal("1") / vol) if vol > 0 else Decimal("1")
        total = sum(inv.values())
        if total <= 0:
            return {}
        return {s: inv[s] / total for s in active}


def weights_to_intents(
    target_weights: dict[str, Decimal],
    current_quantities: dict[str, int],
    prices: dict[str, Decimal],
    nav: Decimal,
) -> list[OrderIntent]:
    """由目标权重、当前持仓、价格、NAV 生成再平衡 OrderIntent（参考 PortfolioAllocator 但更精简）。"""
    intents: list[OrderIntent] = []
    symbols = set(target_weights) | set(current_quantities)
    for symbol in symbols:
        weight = target_weights.get(symbol, Decimal("0"))
        price = prices.get(symbol, Decimal("0"))
        target_value = nav * weight
        if price > 0:
            target_qty = int((target_value / price).to_integral_value())
        else:
            target_qty = 0
        current_qty = current_quantities.get(symbol, 0)
        delta = target_qty - current_qty
        if delta == 0:
            continue
        side = "BUY" if delta > 0 else "SELL"
        intents.append(
            OrderIntent(
                symbol=symbol,
                side=side,
                quantity=abs(delta),
                order_type="LIMIT",
                limit_price=price if price > 0 else None,
                reason="construction_rebalance",
            )
        )
    return intents
