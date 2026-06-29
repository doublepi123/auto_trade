from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from app.platform.events import Event, FillEvent, OrderEvent, OrderIntentEvent

__all__ = ["OrderRecord", "OrderManagementSystem"]


@dataclass
class OrderRecord:
    broker_order_id: str
    symbol: str
    status: str = "SUBMITTED"
    side: str | None = None
    quantity: int | None = None
    order_type: str | None = None
    limit_price: Decimal | None = None
    filled_quantity: int = 0
    avg_fill_price: Decimal | None = None
    fills: list[FillEvent] = field(default_factory=list)
    created_at: Any = None
    updated_at: Any = None

    @property
    def is_open(self) -> bool:
        return self.status in ("SUBMITTED", "PARTIAL_FILLED", "ACKED", "MODIFIED")


class OrderManagementSystem:
    """中央订单管理（参考 Nautilus OMS）：订阅 order_intent/order/fill，跟踪订单生命周期。

    order_intent 为 best-effort 充实 side/quantity/order_type/limit_price（按 symbol 顺序配对最近的 order 事件）。
    """

    def __init__(self) -> None:
        self._orders: dict[str, OrderRecord] = {}
        self._pending_intents: dict[str, deque[OrderIntentEvent]] = {}

    # ---- bus handlers ----
    def on_order_intent(self, event: Event) -> None:
        if not isinstance(event, OrderIntentEvent):
            return
        symbol = event.symbol or ""
        self._pending_intents.setdefault(symbol, deque()).append(event)

    def on_order(self, event: Event) -> None:
        if not isinstance(event, OrderEvent):
            return
        rec = self._orders.get(event.broker_order_id)
        if rec is None:
            rec = OrderRecord(broker_order_id=event.broker_order_id, symbol=event.symbol or "", created_at=event.timestamp)
            self._orders[event.broker_order_id] = rec
            # best-effort enrichment from a pending intent for this symbol
            pending = self._pending_intents.get(event.symbol or "")
            if pending:
                intent = pending.popleft()
                rec.side = intent.side
                rec.quantity = intent.quantity
                rec.order_type = intent.order_type
                rec.limit_price = intent.limit_price
        rec.status = event.status
        if event.filled_quantity and rec.filled_quantity == 0:
            # Only seed filled_quantity from order-event when no fill events
            # have already contributed — prevents overwriting the accumulation
            # done in on_fill.
            rec.filled_quantity = event.filled_quantity
        if event.avg_price is not None and rec.avg_fill_price is None:
            rec.avg_fill_price = event.avg_price
        rec.updated_at = event.timestamp

    def on_fill(self, event: Event) -> None:
        if not isinstance(event, FillEvent):
            return
        rec = self._orders.get(event.broker_order_id)
        if rec is None:
            rec = OrderRecord(broker_order_id=event.broker_order_id, symbol=event.symbol or "", side=event.side, created_at=event.timestamp)
            self._orders[event.broker_order_id] = rec
        rec.fills.append(event)
        rec.filled_quantity += event.quantity
        # weighted average fill price
        prev_value = (rec.avg_fill_price or Decimal("0")) * Decimal(rec.filled_quantity - event.quantity)
        new_value = prev_value + event.price * Decimal(event.quantity)
        rec.avg_fill_price = new_value / Decimal(rec.filled_quantity) if rec.filled_quantity > 0 else None
        if rec.quantity is not None and rec.filled_quantity >= rec.quantity:
            rec.status = "FILLED"
        else:
            rec.status = "PARTIAL_FILLED"
        rec.updated_at = event.timestamp

    def subscribe(self, bus: Any) -> None:
        bus.subscribe("order_intent", self.on_order_intent)
        bus.subscribe("order", self.on_order)
        bus.subscribe("fill", self.on_fill)

    # ---- queries ----
    def get(self, broker_order_id: str) -> OrderRecord | None:
        return self._orders.get(broker_order_id)

    def all(self) -> list[OrderRecord]:
        return list(self._orders.values())

    def by_status(self, status: str) -> list[OrderRecord]:
        return [r for r in self._orders.values() if r.status == status]

    def open_orders(self) -> list[OrderRecord]:
        return [r for r in self._orders.values() if r.is_open]
