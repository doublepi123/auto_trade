from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.platform.context import StrategyContext
from app.platform.events import BarEvent, FillEvent, QuoteEvent


@dataclass(frozen=True)
class OrderIntent:
    symbol: str
    side: str
    quantity: int
    order_type: str = "LIMIT"
    limit_price: Decimal | None = None
    reason: str | None = None


class Strategy(Protocol):
    """Strategy plugin protocol."""

    params: dict[str, Any]

    def on_bar(self, context: StrategyContext, bar: BarEvent) -> list[OrderIntent]:
        ...

    def on_quote(self, context: StrategyContext, quote: QuoteEvent) -> list[OrderIntent]:
        ...

    def on_fill(self, context: StrategyContext, fill: FillEvent) -> list[OrderIntent]:
        ...
