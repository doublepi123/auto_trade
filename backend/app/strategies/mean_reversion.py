from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent


@dataclass
class MeanReversionStrategy:
    params: dict[str, Any]
    _closes: dict[str, deque[Decimal]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "mean_reversion"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["lookback", "entry_z", "exit_z", "quantity"],
            "properties": {
                "lookback": {"type": "integer"},
                "entry_z": {"type": "number"},
                "exit_z": {"type": "number"},
                "quantity": {"type": "integer"},
            },
        }

    def _position_quantity(self, ctx: StrategyContext) -> int:
        position = ctx.positions.get(ctx.symbol, {})
        return int(position.get("quantity", 0))

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        lookback = int(self.params.get("lookback", 20))
        entry_z = float(self.params.get("entry_z", -2.0))
        exit_z = float(self.params.get("exit_z", 0.0))
        quantity = int(self.params.get("quantity", 1))
        current_qty = self._position_quantity(ctx)
        symbol = bar.symbol or ctx.symbol

        closes = self._closes.get(symbol)
        if closes is None:
            closes = deque(maxlen=lookback + 2)
            self._closes[symbol] = closes
        closes.append(bar.close)

        if len(closes) < lookback:
            return []

        window = list(closes)[-lookback:]
        count = Decimal(lookback)
        mean = sum(window, start=Decimal("0")) / count
        variance = sum(
            ((close - mean) ** 2 for close in window),
            start=Decimal("0"),
        ) / count
        standard_deviation = variance.sqrt()
        if standard_deviation == 0:
            return []

        z_score = float((bar.close - mean) / standard_deviation)
        if current_qty <= 0 and z_score <= entry_z:
            return [
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="zscore_oversold",
                )
            ]
        if current_qty > 0 and z_score >= exit_z:
            return [
                OrderIntent(
                    symbol=symbol,
                    side="SELL",
                    quantity=current_qty,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="zscore_reverted_to_mean",
                )
            ]
        return []

    def on_quote(self, _ctx: StrategyContext, _quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, _ctx: StrategyContext, _fill: FillEvent) -> list[OrderIntent]:
        return []
