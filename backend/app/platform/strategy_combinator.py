from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent
from app.platform.sdk import OrderIntent, Strategy

__all__ = ["StrategyCombinator"]


@dataclass
class _Aggregate:
    quantity: int = 0
    limit_price: Any = None
    order_type: str = "LIMIT"
    contributors: int = 0


@dataclass
class StrategyCombinator:
    """多策略组合 / alpha 池（参考 Nautilus 多策略、Lean AlphaModel 合流）。

    持有 (strategy, weight) 列表；每个事件把子策略的 OrderIntent 按 (symbol, side) 聚合：
    quantity = round(sum(child.quantity * weight))；limit_price 取首个贡献者。
    自身实现 Strategy Protocol，可作为单个策略注入 PlatformRunner。
    """

    strategies: list[tuple[Strategy, float]]
    name: str = "combinator"
    params: dict[str, Any] = field(default_factory=dict)

    @property
    def version(self) -> str:
        return "1.0.0"

    @property
    def parameter_schema(self) -> dict[str, Any]:
        return {"type": "object", "properties": {}}

    def _fuse(self, intents: list[OrderIntent]) -> list[OrderIntent]:
        agg: dict[tuple[str, str], _Aggregate] = {}
        for intent in intents:
            key = (intent.symbol, intent.side)
            slot = agg.setdefault(key, _Aggregate())
            slot.quantity += intent.quantity  # raw child quantity (weight applied below)
            if slot.limit_price is None and intent.limit_price is not None:
                slot.limit_price = intent.limit_price
            slot.order_type = intent.order_type
            slot.contributors += 1
        # NOTE: weighting is applied per-strategy BEFORE aggregation via _weighted_children
        out: list[OrderIntent] = []
        for (symbol, side), slot in agg.items():
            if slot.quantity <= 0:
                continue
            out.append(
                OrderIntent(
                    symbol=symbol,
                    side=side,
                    quantity=slot.quantity,
                    order_type=slot.order_type,
                    limit_price=slot.limit_price,
                    reason="combinator_fused",
                )
            )
        return out

    def _weighted_children(self, strat: Strategy, raw: list[OrderIntent], weight: float) -> list[OrderIntent]:
        if weight == 1.0:
            return list(raw)
        scaled: list[OrderIntent] = []
        for intent in raw:
            scaled.append(
                OrderIntent(
                    symbol=intent.symbol,
                    side=intent.side,
                    quantity=max(0, round(intent.quantity * weight)),
                    order_type=intent.order_type,
                    limit_price=intent.limit_price,
                    reason=intent.reason,
                )
            )
        return scaled

    def _collect(self, child_lists: list[tuple[Strategy, float, list[OrderIntent]]]) -> list[OrderIntent]:
        merged: list[OrderIntent] = []
        for _strat, weight, raw in child_lists:
            merged.extend(self._weighted_children(_strat, raw, weight))
        return self._fuse(merged)

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        collected: list[tuple[Strategy, float, list[OrderIntent]]] = []
        for strat, weight in self.strategies:
            collected.append((strat, weight, strat.on_bar(ctx, bar)))
        return self._collect(collected)

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        collected: list[tuple[Strategy, float, list[OrderIntent]]] = []
        for strat, weight in self.strategies:
            collected.append((strat, weight, strat.on_quote(ctx, quote)))
        return self._collect(collected)

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        collected: list[tuple[Strategy, float, list[OrderIntent]]] = []
        for strat, weight in self.strategies:
            collected.append((strat, weight, strat.on_fill(ctx, fill)))
        return self._collect(collected)
