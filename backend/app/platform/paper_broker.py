from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent, QuoteEvent
from app.platform.paper_order_state import PaperOrderState
from app.platform.sdk import OrderIntent


@dataclass
class PaperBrokerConfig:
    slippage_ticks: Decimal = Decimal("0.01")
    commission_rate: Decimal = Decimal("0.0005")
    partial_fill_probability: float = 1.0
    latency_ms: int = 0


class PaperBroker:
    """真实成交仿真的 Paper Broker。支持 LIMIT 单按 bar 撮合、partial fill、滑点、费用。"""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        config: PaperBrokerConfig | None = None,
    ) -> None:
        self._orders: dict[str, PaperOrderState] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._config = config or PaperBrokerConfig()

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order_id = f"paper-{uuid4().hex[:8]}"
        self._orders[order_id] = PaperOrderState(order_id=order_id, intent=intent)
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol,
            broker_order_id=order_id,
            status="SUBMITTED",
        )

    def cancel(self, order_id: str, timestamp: datetime | None = None) -> OrderEvent:
        order = self._orders.get(order_id)
        if order and order.status in ("SUBMITTED", "PARTIAL_FILLED"):
            order.status = "CANCELLED"
            return OrderEvent(
                timestamp=timestamp or self._clock(),
                source=EventSource.BROKER,
                symbol=order.intent.symbol,
                broker_order_id=order_id,
                status="CANCELLED",
            )
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=order.intent.symbol if order else None,
            broker_order_id=order_id,
            status="REJECTED",
            reason="cannot cancel",
        )

    def modify(self, order_id: str, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order = self._orders.get(order_id)
        if order and order.status in ("SUBMITTED", "PARTIAL_FILLED"):
            order.intent = intent
            return OrderEvent(
                timestamp=timestamp or self._clock(),
                source=EventSource.BROKER,
                symbol=intent.symbol,
                broker_order_id=order_id,
                status="MODIFIED",
            )
        return OrderEvent(
            timestamp=timestamp or self._clock(),
            source=EventSource.BROKER,
            symbol=intent.symbol if order else None,
            broker_order_id=order_id,
            status="REJECTED",
            reason="cannot modify",
        )

    def on_bar(self, bar: BarEvent) -> list[FillEvent]:
        fills: list[FillEvent] = []
        for order in list(self._orders.values()):
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            intent = order.intent
            if intent.symbol != bar.symbol:
                continue
            if intent.order_type != "LIMIT" or intent.limit_price is None:
                continue

            if intent.side == "BUY" and bar.low <= intent.limit_price:
                fill_price = min(bar.open, intent.limit_price) + self._config.slippage_ticks
            elif intent.side == "SELL" and bar.high >= intent.limit_price:
                fill_price = max(bar.open, intent.limit_price) - self._config.slippage_ticks
            else:
                continue

            fill_qty = self._compute_fill_quantity(order, bar)
            if fill_qty <= 0:
                continue
            commission = fill_price * Decimal(fill_qty) * self._config.commission_rate
            order.fill(fill_qty, fill_price, slippage=self._config.slippage_ticks, commission=commission)
            fills.append(
                FillEvent(
                    timestamp=bar.timestamp,
                    source=EventSource.BROKER,
                    symbol=bar.symbol,
                    broker_order_id=order.order_id,
                    side=intent.side,
                    quantity=fill_qty,
                    price=fill_price,
                    slippage=self._config.slippage_ticks,
                    commission=commission,
                    partial=order.status == "PARTIAL_FILLED",
                )
            )
        return fills

    def _compute_fill_quantity(self, order: PaperOrderState, bar: BarEvent) -> int:
        remaining = order.remaining_quantity
        if self._config.partial_fill_probability >= 1.0:
            return remaining
        portion = max(1, int(remaining * self._config.partial_fill_probability))
        return min(portion, remaining)

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
