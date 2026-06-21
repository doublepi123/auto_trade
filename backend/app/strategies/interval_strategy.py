from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent


class IntervalStrategy:
    """Simple interval strategy: buy at buy_low, sell at sell_high."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.params = params or {}

    def on_bar(self, context: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        intents: list[OrderIntent] = []
        buy_low = Decimal(str(self.params.get("buy_low", "0")))
        sell_high = Decimal(str(self.params.get("sell_high", "0")))
        quantity = int(self.params.get("quantity", 0) or 0)
        pos = context.positions.get(bar.symbol, {"quantity": 0})
        qty = pos["quantity"]

        if bar.close <= buy_low and qty <= 0 and quantity > 0:
            intents.append(
                OrderIntent(
                    symbol=bar.symbol,
                    side="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="interval_buy_low",
                )
            )
        elif bar.close >= sell_high and qty > 0:
            intents.append(
                OrderIntent(
                    symbol=bar.symbol,
                    side="SELL",
                    quantity=abs(qty),
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="interval_sell_high",
                )
            )
        return intents

    def on_quote(self, context: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, context: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        return []
