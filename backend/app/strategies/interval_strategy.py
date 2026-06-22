from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent, Strategy


@dataclass
class IntervalStrategy:
    """区间交易策略插件。Phase 1 简化实现：
    - 空仓且 bar.close <= buy_low -> BUY quantity
    - 持多且 bar.close >= sell_high -> SELL 全部持仓
    - 持仓数量从 ctx.positions[symbol].quantity 读取
    """

    params: dict[str, Any]

    @property
    def name(self) -> str:
        return "interval"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["buy_low", "sell_high", "quantity"],
            "properties": {
                "buy_low": {"type": "number"},
                "sell_high": {"type": "number"},
                "quantity": {"type": "integer"},
            },
        }

    def _position_quantity(self, ctx: StrategyContext) -> int:
        pos = ctx.positions.get(ctx.symbol, {})
        return int(pos.get("quantity", 0))

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        buy_low = Decimal(self.params["buy_low"])
        sell_high = Decimal(self.params["sell_high"])
        quantity = int(self.params["quantity"])
        current_qty = self._position_quantity(ctx)
        symbol = bar.symbol or ctx.symbol

        if bar.close <= buy_low and current_qty <= 0:
            return [
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="price_below_buy_low",
                )
            ]
        if bar.close >= sell_high and current_qty > 0:
            return [
                OrderIntent(
                    symbol=symbol,
                    side="SELL",
                    quantity=current_qty,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="price_above_sell_high",
                )
            ]
        return []

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        return []
