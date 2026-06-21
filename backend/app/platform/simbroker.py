from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Callable

from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent
from app.platform.sdk import OrderIntent


class SimBroker:
    """Simple backtest broker: immediately fills limit orders at bar close."""

    def __init__(self, clock: Callable[[], datetime] | None = None) -> None:
        self.clock = clock or (lambda: datetime.now())
        self._pending: list[dict[str, Any]] = []
        self._counter = 0

    def submit(self, intent: OrderIntent) -> OrderEvent:
        self._counter += 1
        self._pending.append({
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "limit_price": intent.limit_price,
        })
        return OrderEvent(
            timestamp=self.clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            side=intent.side,
            quantity=intent.quantity,
            order_type=intent.order_type,
            limit_price=intent.limit_price,
            status="SUBMITTED",
        )

    def on_bar(self, bar: BarEvent) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for pending in self._pending:
            fills.append(
                FillEvent(
                    timestamp=bar.timestamp,
                    source=EventSource.BROKER,
                    symbol=pending["symbol"],
                    side=pending["side"],
                    quantity=pending["quantity"],
                    price=bar.close,
                )
            )
        self._pending.clear()
        return fills
