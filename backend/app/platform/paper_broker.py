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
from app.platform.fill_model import FillModel
from app.platform.latency import LatencyModel
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
    partial_fill_probability: float = 1.0  # portion of remaining quantity to fill per bar (0.0-1.0), not a true probability
    latency_ms: int = 0
    fill_model: FillModel | None = None
    latency_model: LatencyModel | None = None


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
        self._bar_counter: dict[str, int] = {}

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
        latency = self._config.latency_model
        if latency is not None and latency.submit_delay() > 0:
            # P192: order waits in QUEUED until submit_due hits 0 via on_bar promotion
            state = PaperOrderState(
                order_id=order_id,
                intent=intent,
                status="QUEUED",
                submit_due=latency.submit_delay(),
            )
            self._orders[order_id] = state
            self._persist(state)
            return OrderEvent(
                timestamp=timestamp or self._clock(),
                source=EventSource.BROKER,
                symbol=intent.symbol,
                broker_order_id=order_id,
                status="QUEUED",
            )
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
            old_type = order.intent.order_type
            order.intent = intent
            # Reset trailing stop state when switching to (or reconfiguring) a
            # TRAILING order so the ratchet starts fresh from the current bar.
            if intent.order_type == "TRAILING" or old_type == "TRAILING":
                self._trailing_stops.pop(order_id, None)
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
        latency = self._config.latency_model
        fill_delay = latency.fill_delay() if latency is not None else 0

        symbol = bar.symbol or ""
        self._bar_counter[symbol] = self._bar_counter.get(symbol, 0) + 1

        # P192 step 2: promote QUEUED orders whose submit_due has elapsed.
        for order in list(self._orders.values()):
            if order.status != "QUEUED":
                continue
            if order.intent.symbol != symbol:
                continue
            order.submit_due -= 1
            if order.submit_due <= 0:
                order.status = "SUBMITTED"
                self._persist(order)

        # P192 step 3: release held fills (fill latency) before matching new bars.
        for order in list(self._orders.values()):
            if order.pending_fill_price is None:
                continue
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            if order.intent.symbol != symbol:
                continue
            # update trailing stop even while fill is held
            self._update_trailing(order, bar)
            order.fill_due -= 1
            if order.fill_due <= 0:
                trigger_price = order.pending_fill_price
                order.pending_fill_price = None
                fill_evt = self._execute_fill(order, trigger_price, bar)
                if fill_evt is not None:
                    fills.append(fill_evt)
                    if order.status == "FILLED" and order.intent.linked_order_id:
                        cancelled_partners.append(order.intent.linked_order_id)

        # P192 step 4: existing matching loop, optionally deferring fills into held.
        for order in list(self._orders.values()):
            if order.status not in ("SUBMITTED", "PARTIAL_FILLED"):
                continue
            intent = order.intent
            if intent.symbol != symbol:
                continue
            trigger_price = self._match_price(order, bar)
            if trigger_price is None:
                # update trailing stop even when not triggered
                self._update_trailing(order, bar)
                continue
            if fill_delay > 0:
                # hold the fill: defer emission until fill_due elapses
                order.pending_fill_price = trigger_price
                order.fill_due = fill_delay
                self._persist(order)
                continue
            fill_evt = self._execute_fill(order, trigger_price, bar)
            if fill_evt is not None:
                fills.append(fill_evt)
                if order.status == "FILLED" and intent.linked_order_id:
                    cancelled_partners.append(intent.linked_order_id)
        for partner_id in cancelled_partners:
            partner = self._orders.get(partner_id)
            if partner and partner.status in ("SUBMITTED", "PARTIAL_FILLED"):
                partner.status = "CANCELLED"
                self._persist(partner)
        return fills

    def _execute_fill(
        self,
        order: PaperOrderState,
        trigger_price: Decimal,
        bar: BarEvent,
    ) -> FillEvent | None:
        """Compute slippage/commission, mutate order via order.fill, persist, return FillEvent.

        Returns None if no fillable quantity remains.
        """
        intent = order.intent
        fill_qty = self._compute_fill_quantity(order, bar)
        if fill_qty <= 0:
            return None
        fm = self._config.fill_model
        if fm is not None:
            slip = fm.slippage_model.slippage(intent.side, trigger_price, bar, fill_qty)
            fill_price = (trigger_price + slip) if intent.side == "BUY" else (trigger_price - slip)
            commission = fm.commission_model.commission(fill_qty, fill_price)
            slip_reported = slip
        else:
            slip_reported = self._config.slippage_ticks
            if intent.side == "BUY":
                fill_price = trigger_price + slip_reported
            else:
                fill_price = trigger_price - slip_reported
            commission = fill_price * Decimal(fill_qty) * self._config.commission_rate
        order.fill(fill_qty, fill_price, slippage=slip_reported, commission=commission)
        self._persist(order)
        return FillEvent(
            timestamp=bar.timestamp,
            source=EventSource.BROKER,
            symbol=bar.symbol or "",
            broker_order_id=order.order_id,
            side=intent.side,
            quantity=fill_qty,
            price=fill_price,
            slippage=slip_reported,
            commission=commission,
            partial=order.status == "PARTIAL_FILLED",
        )

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
        """Determine fill quantity per bar.  ``partial_fill_probability`` is a
        deterministic portion (0-1) of the remaining quantity, not a true
        probability — it fills ``portion * remaining`` (min 1) each bar."""
        remaining = order.remaining_quantity
        if self._config.partial_fill_probability >= 1.0:
            return remaining
        portion = max(1, int(remaining * self._config.partial_fill_probability))
        return min(portion, remaining)

    def on_quote(self, quote: QuoteEvent) -> list[FillEvent]:
        return []
