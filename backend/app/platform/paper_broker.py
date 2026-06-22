from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Callable
from uuid import uuid4

from sqlalchemy.orm import Session

from app.models import PaperOrder as PaperOrderModel
from app.platform.events import BarEvent, EventSource, FillEvent, OrderEvent, QuoteEvent
from app.platform.paper_order_state import PaperOrderState
from app.platform.sdk import OrderIntent


def _intent_to_json(intent: OrderIntent) -> str:
    return json.dumps(
        {
            "symbol": intent.symbol,
            "side": intent.side,
            "quantity": intent.quantity,
            "order_type": intent.order_type,
            "limit_price": str(intent.limit_price) if intent.limit_price is not None else None,
            "reason": intent.reason,
            "stop_price": str(intent.stop_price) if intent.stop_price is not None else None,
            "trailing_offset": str(intent.trailing_offset) if intent.trailing_offset is not None else None,
            "linked_order_id": intent.linked_order_id,
        }
    )


def _intent_from_json(raw: str) -> OrderIntent:
    data = json.loads(raw)
    return OrderIntent(
        symbol=data["symbol"],
        side=data["side"],
        quantity=int(data["quantity"]),
        order_type=data["order_type"],
        limit_price=Decimal(data["limit_price"]) if data.get("limit_price") is not None else None,
        reason=data.get("reason", ""),
        stop_price=Decimal(data["stop_price"]) if data.get("stop_price") is not None else None,
        trailing_offset=Decimal(data["trailing_offset"]) if data.get("trailing_offset") is not None else None,
        linked_order_id=data.get("linked_order_id"),
    )


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
        session: Session | None = None,
    ) -> None:
        self._orders: dict[str, PaperOrderState] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._config = config or PaperBrokerConfig()
        self._session = session
        self._trailing_stops: dict[str, Decimal] = {}

    def _persist(self, order: PaperOrderState) -> None:
        if self._session is None:
            return
        intent = order.intent
        row = self._session.query(PaperOrderModel).filter_by(broker_order_id=order.order_id).first()
        if row is None:
            row = PaperOrderModel(
                broker_order_id=order.order_id,
                symbol=intent.symbol,
                side=intent.side,
                quantity=intent.quantity,
                filled_quantity=order.filled_quantity,
                limit_price=float(intent.limit_price) if intent.limit_price is not None else None,
                status=order.status,
                intent_json=_intent_to_json(intent),
            )
            self._session.add(row)
        else:
            row.filled_quantity = order.filled_quantity
            row.status = order.status
            row.limit_price = float(intent.limit_price) if intent.limit_price is not None else None
            row.symbol = intent.symbol
            row.side = intent.side
            row.quantity = intent.quantity
            row.intent_json = _intent_to_json(intent)
        self._session.commit()

    @classmethod
    def from_db(
        cls,
        session: Session,
        clock: Callable[[], datetime] | None = None,
        config: PaperBrokerConfig | None = None,
    ) -> PaperBroker:
        broker = cls(clock=clock, config=config, session=session)
        rows = session.query(PaperOrderModel).filter(
            PaperOrderModel.status.in_(("SUBMITTED", "PARTIAL_FILLED"))
        ).all()
        for row in rows:
            intent = _intent_from_json(row.intent_json)
            state = PaperOrderState(
                order_id=row.broker_order_id,
                intent=intent,
                status=row.status,
                filled_quantity=row.filled_quantity,
            )
            broker._orders[row.broker_order_id] = state
        return broker

    def submit(self, intent: OrderIntent, timestamp: datetime | None = None) -> OrderEvent:
        order_id = f"paper-{uuid4().hex[:8]}"
        state = PaperOrderState(order_id=order_id, intent=intent)
        self._orders[order_id] = state
        self._persist(state)
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
            self._persist(order)
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
            self._persist(order)
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
        cancelled_partners: list[str] = []
        for order in list(self._orders.values()):
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            intent = order.intent
            if intent.symbol != bar.symbol:
                continue
            trigger_price = self._match_price(order, bar)
            if trigger_price is None:
                # update trailing stop even when not triggered
                self._update_trailing(order, bar)
                continue
            fill_qty = self._compute_fill_quantity(order, bar)
            if fill_qty <= 0:
                continue
            if intent.side == "BUY":
                fill_price = trigger_price + self._config.slippage_ticks
            else:
                fill_price = trigger_price - self._config.slippage_ticks
            commission = fill_price * Decimal(fill_qty) * self._config.commission_rate
            order.fill(fill_qty, fill_price, slippage=self._config.slippage_ticks, commission=commission)
            self._persist(order)
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
            if order.status == "FILLED" and intent.linked_order_id:
                cancelled_partners.append(intent.linked_order_id)
        for partner_id in cancelled_partners:
            partner = self._orders.get(partner_id)
            if partner and partner.status in ("SUBMITTED", "PARTIAL_FILLED"):
                partner.status = "CANCELLED"
                self._persist(partner)
        return fills

    def _match_price(self, order: PaperOrderState, bar: BarEvent) -> Decimal | None:
        """Return the fill trigger price for LIMIT/STOP/TRAILING, or None if not triggered."""
        intent = order.intent
        if intent.order_type == "LIMIT" and intent.limit_price is not None:
            if intent.side == "BUY" and bar.low <= intent.limit_price:
                return min(bar.open, intent.limit_price)
            if intent.side == "SELL" and bar.high >= intent.limit_price:
                return max(bar.open, intent.limit_price)
            return None
        if intent.order_type == "STOP" and intent.stop_price is not None:
            if intent.side == "BUY" and bar.high >= intent.stop_price:
                return max(bar.open, intent.stop_price)
            if intent.side == "SELL" and bar.low <= intent.stop_price:
                return min(bar.open, intent.stop_price)
            return None
        if intent.order_type == "TRAILING" and intent.trailing_offset is not None:
            stop = self._trailing_stops.get(order.order_id)
            if stop is None:
                return None
            if intent.side == "SELL" and bar.low <= stop:
                return min(bar.open, stop)
            return None
        return None

    def _update_trailing(self, order: PaperOrderState, bar: BarEvent) -> None:
        intent = order.intent
        if intent.order_type != "TRAILING" or intent.trailing_offset is None:
            return
        # SELL trailing stop ratchets up as price rises; anchor = high - offset
        candidate = bar.high - intent.trailing_offset
        current = self._trailing_stops.get(order.order_id)
        if current is None or candidate > current:
            self._trailing_stops[order.order_id] = candidate

    def _compute_fill_quantity(self, order: PaperOrderState, bar: BarEvent) -> int:
        remaining = order.remaining_quantity
        if self._config.partial_fill_probability >= 1.0:
            return remaining
        portion = max(1, int(remaining * self._config.partial_fill_probability))
        return min(portion, remaining)

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
