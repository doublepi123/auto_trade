from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol, runtime_checkable

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str  # "BUY" or "SELL"
    quantity: int
    order_type: str  # "MARKET" or "LIMIT" initially
    limit_price: Decimal | None = None
    reason: str = ""


@runtime_checkable
class Strategy(Protocol):
    params: dict[str, Any]

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def parameter_schema(self) -> dict[str, Any]: ...

    def on_bar(self, ctx: StrategyContext, bar: BarEvent) -> list[OrderIntent]: ...

    def on_quote(self, ctx: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]: ...

    def on_fill(self, ctx: StrategyContext, fill: FillEvent) -> list[OrderIntent]: ...
