from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent, QuoteEvent
from app.platform.sdk import OrderIntent


@dataclass
class _PendingOrder:
    order_id: str
    intent: OrderIntent
    status: str = "SUBMITTED"
    filled_quantity: int = 0


class SimBroker:
    """简化回测撮合器。Phase 1 仅支持限价单按 bar 触发全部成交。"""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self._orders: dict[str, _PendingOrder] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order_id = f"sim-{uuid4().hex[:8]}"
        self._orders[order_id] = _PendingOrder(order_id=order_id, intent=intent)
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            broker_order_id=order_id,
            status="SUBMITTED",
        )

    def cancel(self, order_id: str) -> OrderEvent:
        order = self._orders.get(order_id)
        if order and order.status == "SUBMITTED":
            order.status = "CANCELLED"
            return OrderEvent(
                timestamp=self._clock(),
                source=EventSource.BROKER,
                symbol=order.intent.symbol,
                broker_order_id=order_id,
                status="CANCELLED",
            )
        return OrderEvent(
            timestamp=self._clock(),
            source=EventSource.BROKER,
            symbol=order.intent.symbol if order else None,
            broker_order_id=order_id,
            status="REJECTED",
        )

    def on_bar(self, bar: BarEvent) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for order in list(self._orders.values()):
            if order.status != "SUBMITTED":
                continue
            intent = order.intent
            if intent.symbol != bar.symbol:
                continue
            if intent.order_type != "LIMIT" or intent.limit_price is None:
                continue

            if intent.side == "BUY" and bar.low <= intent.limit_price:
                fill_price = min(bar.open, intent.limit_price)
            elif intent.side == "SELL" and bar.high >= intent.limit_price:
                fill_price = max(bar.open, intent.limit_price)
            else:
                continue

            order.status = "FILLED"
            order.filled_quantity = intent.quantity
            fills.append(
                FillEvent(
                    timestamp=bar.timestamp,
                    source=EventSource.BROKER,
                    symbol=bar.symbol,
                    broker_order_id=order.order_id,
                    side=intent.side,
                    quantity=intent.quantity,
                    price=fill_price,
                )
            )
        return fills

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
