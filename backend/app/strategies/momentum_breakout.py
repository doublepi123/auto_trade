from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent, Strategy


@dataclass
class MomentumBreakoutStrategy:
    """Long-only Donchian breakout strategy with mutable per-symbol state."""

    params: dict[str, Any]
    _bars: dict[str, deque[BarEvent]] = field(default_factory=dict)
    _highest_closes: dict[str, Decimal] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "momentum_breakout"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["channel_period", "atr_period", "atr_multiplier", "quantity"],
            "properties": {
                "channel_period": {"type": "integer", "minimum": 1, "default": 20},
                "atr_period": {"type": "integer", "minimum": 1, "default": 14},
                "atr_multiplier": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "default": 2.0,
                },
                "quantity": {"type": "integer", "minimum": 1, "default": 1},
            },
        }

    def _position_quantity(self, ctx: StrategyContext) -> int:
        pos = ctx.positions.get(ctx.symbol, {})
        return int(pos.get("quantity", 0))

    def _average_true_range(self, bars: deque[BarEvent], atr_period: int) -> Decimal:
        recent_bars = list(bars)[-(atr_period + 1) :]
        true_ranges: list[Decimal] = []
        for previous, current in zip(recent_bars, recent_bars[1:]):
            true_ranges.append(
                max(
                    current.high - current.low,
                    abs(current.high - previous.close),
                    abs(current.low - previous.close),
                )
            )
        return sum(true_ranges, Decimal("0")) / Decimal(atr_period)

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        channel_period = int(self.params.get("channel_period", 20))
        atr_period = int(self.params.get("atr_period", 14))
        atr_multiplier = Decimal(str(self.params.get("atr_multiplier", 2.0)))
        quantity = int(self.params.get("quantity", 1))
        current_qty = self._position_quantity(ctx)
        symbol = bar.symbol or ctx.symbol
        history_size = max(channel_period, atr_period) + 2
        bars = self._bars.get(symbol)
        if bars is None:
            bars = deque(maxlen=history_size)
            self._bars[symbol] = bars
        bars.append(bar)

        if current_qty > 0:
            highest_close = max(self._highest_closes.get(symbol, bar.close), bar.close)
            self._highest_closes[symbol] = highest_close
            if len(bars) < atr_period + 1:
                return []
            atr = self._average_true_range(bars, atr_period)
            trailing_stop = highest_close - atr_multiplier * atr
            if bar.close <= trailing_stop:
                return [
                    OrderIntent(
                        symbol=symbol,
                        side="SELL",
                        quantity=current_qty,
                        order_type="LIMIT",
                        limit_price=bar.close,
                        reason="atr_trailing_stop",
                    )
                ]
            return []

        self._highest_closes.pop(symbol, None)
        if len(bars) < channel_period + 1:
            return []
        upper_channel = max(
            previous.high for previous in list(bars)[-(channel_period + 1) : -1]
        )
        if bar.close > upper_channel:
            self._highest_closes[symbol] = bar.close
            return [
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="donchian_breakout",
                )
            ]
        return []

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        if self._position_quantity(ctx) <= 0:
            self._highest_closes.pop(fill.symbol or ctx.symbol, None)
        return []
