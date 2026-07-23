from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent, Strategy


@dataclass
class TrendFollowingStrategy:
    """Dual-moving-average strategy with mutable per-symbol bar history."""

    params: dict[str, Any]
    _bars: dict[str, deque[BarEvent]] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return "trend_following"

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {
            "type": "object",
            "required": ["fast_period", "slow_period", "atr_period", "quantity"],
            "properties": {
                "fast_period": {"type": "integer"},
                "slow_period": {"type": "integer"},
                "atr_period": {"type": "integer"},
                "atr_threshold_pct": {"type": "number"},
                "quantity": {"type": "integer"},
            },
        }

    def _position_quantity(self, ctx: StrategyContext) -> int:
        pos = ctx.positions.get(ctx.symbol, {})
        return int(pos.get("quantity", 0))

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        fast_period = int(self.params.get("fast_period", 10))
        slow_period = int(self.params.get("slow_period", 30))
        atr_period = int(self.params.get("atr_period", 14))
        atr_threshold_pct = float(self.params.get("atr_threshold_pct", 0.0))
        quantity = int(self.params.get("quantity", 1))
        symbol = bar.symbol or ctx.symbol

        bars = self._bars.get(symbol)
        if bars is None:
            bars = deque(maxlen=slow_period + 4)
            self._bars[symbol] = bars
        bars.append(bar)

        if len(bars) < max(fast_period, slow_period) + 1:
            return []

        bar_list = list(bars)
        closes = [item.close for item in bar_list]
        current_fast = sum(closes[-fast_period:], Decimal("0")) / Decimal(fast_period)
        current_slow = sum(closes[-slow_period:], Decimal("0")) / Decimal(slow_period)
        previous_fast = sum(closes[-fast_period - 1 : -1], Decimal("0")) / Decimal(fast_period)
        previous_slow = sum(closes[-slow_period - 1 : -1], Decimal("0")) / Decimal(slow_period)
        current_qty = self._position_quantity(ctx)

        golden_cross = previous_fast <= previous_slow and current_fast > current_slow
        if golden_cross and current_qty == 0:
            if atr_threshold_pct > 0:
                if len(bar_list) < atr_period + 1:
                    return []
                true_ranges: list[Decimal] = []
                for index in range(len(bar_list) - atr_period, len(bar_list)):
                    current_bar = bar_list[index]
                    previous_close = bar_list[index - 1].close
                    true_ranges.append(
                        max(
                            current_bar.high - current_bar.low,
                            abs(current_bar.high - previous_close),
                            abs(current_bar.low - previous_close),
                        )
                    )
                atr = sum(true_ranges, Decimal("0")) / Decimal(atr_period)
                threshold = Decimal(str(atr_threshold_pct))
                if atr / bar.close <= threshold:
                    return []
            return [
                OrderIntent(
                    symbol=symbol,
                    side="BUY",
                    quantity=quantity,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="ma_golden_cross",
                )
            ]

        death_cross = previous_fast >= previous_slow and current_fast < current_slow
        if death_cross and current_qty > 0:
            return [
                OrderIntent(
                    symbol=symbol,
                    side="SELL",
                    quantity=current_qty,
                    order_type="LIMIT",
                    limit_price=bar.close,
                    reason="ma_death_cross",
                )
            ]
        return []

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        return []

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        return []
