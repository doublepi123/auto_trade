from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from threading import RLock
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from app.core.broker import BrokerGateway, OrderResult, Quote
    from app.core.notify import ServerChanNotifier
    from app.core.risk import RiskController

logger = logging.getLogger("auto_trade.services.trade_execution_service")

_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_FAILED_ORDER_STATUSES = {"REJECTED", "CANCELLED"}
_EngineSnapshot = tuple[object, float, datetime | None]


@dataclass(frozen=True)
class OrderStatus:
    broker_order_id: str
    status: str
    executed_quantity: Decimal = Decimal("0")
    executed_price: Decimal = Decimal("0")


@dataclass(frozen=True)
class _PendingOrder:
    broker: BrokerGateway
    broker_order_id: str
    symbol: str
    action: str
    quantity: Decimal
    price: Decimal
    engine_snapshot: _EngineSnapshot | None
    avg_price: Decimal | None = None
    next_status_check_at: float = 0.0


class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float, str], None],
        update_order_status: Callable[[str, str, datetime | None, float | None, float | None], None],
        record_risk_event: Callable[[str], None],
    ) -> None:
        self._record_order = record_order
        self._update_order_status = update_order_status
        self._record_risk_event = record_risk_event
        self._state_lock = RLock()
        self._pending_order: _PendingOrder | None = None
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0

    @property
    def has_pending_order(self) -> bool:
        with self._state_lock:
            return self._pending_order is not None

    def reconcile(
        self,
        risk: RiskController | None = None,
        notifier: ServerChanNotifier | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> None:
        with self._state_lock:
            pending = self._pending_order
        if pending is not None:
            self._reconcile_pending_order(
                pending,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )

    def execute(
        self,
        action: str,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> OrderStatus | None:
        if action == "BUY":
            return self._execute_buy(symbol, quote, broker, risk, notifier, cash_currency, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "SELL":
            return self._execute_sell(symbol, quote, broker, risk, notifier, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "SELL_SHORT":
            return self._execute_sell_short(symbol, quote, broker, risk, notifier, cash_currency, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "BUY_TO_COVER":
            return self._execute_buy_to_cover(symbol, quote, broker, risk, notifier, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        logger.warning("unknown action: %s", action)
        return None

    def _execute_buy(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> OrderStatus | None:
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return None
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return None

        result = broker.submit_limit_order(symbol, "BUY", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            self._track_pending_order("BUY", result, broker, engine_snapshot)
            logger.info("BUY pending: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        if self._handle_terminal_fill_result("BUY", result, order_status, broker, risk, notifier, engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event):
            return order_status

        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status, risk, notify_risk_event)
            logger.warning("BUY not filled: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._safe_notify_order(notifier, "BUY", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        logger.info("BUY: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        return order_status

    def _execute_sell(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return None

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return None

        result = broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            self._track_pending_order("SELL", result, broker, engine_snapshot, avg_price=long_pos.avg_price)
            logger.info("SELL pending: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        if self._handle_terminal_fill_result("SELL", result, order_status, broker, risk, notifier, engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, avg_price=long_pos.avg_price, notify_risk_event=notify_risk_event):
            return order_status

        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status, risk, notify_risk_event)
            logger.warning("SELL not filled: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else long_pos.quantity
        pnl = float((fill_price - long_pos.avg_price) * fill_qty)
        self._safe_notify_order(notifier, "SELL", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        risk.record_trade(pnl)
        logger.info("SELL: %s qty=%s price=%s pnl=%s", symbol, fill_qty, fill_price, pnl)
        return order_status

    def _execute_sell_short(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        cash_currency: str,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> OrderStatus | None:
        cash = broker.get_cash(cash_currency)
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return None

        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return None

        result = broker.submit_limit_order(symbol, "SELL", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "SELL_SHORT", float(qty), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            self._track_pending_order("SELL_SHORT", result, broker, engine_snapshot)
            logger.info("SELL_SHORT pending: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        if self._handle_terminal_fill_result("SELL_SHORT", result, order_status, broker, risk, notifier, engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event):
            return order_status

        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status, risk, notify_risk_event)
            logger.warning("SELL_SHORT not filled: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._safe_notify_order(notifier, "SELL_SHORT", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        logger.info("SELL_SHORT: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        return order_status

    def _execute_buy_to_cover(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: ServerChanNotifier,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return None

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return None

        result = broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "BUY_TO_COVER", float(pos.quantity), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            self._track_pending_order("BUY_TO_COVER", result, broker, engine_snapshot, avg_price=pos.avg_price)
            logger.info("BUY_TO_COVER pending: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        if self._handle_terminal_fill_result("BUY_TO_COVER", result, order_status, broker, risk, notifier, engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, avg_price=pos.avg_price, notify_risk_event=notify_risk_event):
            return order_status

        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status, risk, notify_risk_event)
            logger.warning("BUY_TO_COVER not filled: %s status=%s", result.broker_order_id, order_status.status)
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else pos.quantity
        pnl = float((pos.avg_price - fill_price) * fill_qty)
        self._safe_notify_order(notifier, "BUY_TO_COVER", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        risk.record_trade(pnl)
        logger.info("BUY_TO_COVER: %s qty=%s price=%s pnl=%s", symbol, fill_qty, fill_price, pnl)
        return order_status

    @staticmethod
    def _order_status_is_live(result: object) -> bool:
        return getattr(result, "status", "SUBMITTED") in _LIVE_ORDER_STATUSES

    def _track_pending_order(
        self,
        action: str,
        result: OrderResult,
        broker: BrokerGateway,
        engine_snapshot: _EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
    ) -> None:
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=result.broker_order_id,
            symbol=result.symbol,
            action=action,
            quantity=result.quantity,
            price=result.price,
            engine_snapshot=engine_snapshot,
            avg_price=avg_price,
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
        )
        with self._state_lock:
            self._pending_order = pending

    def _clear_pending_order(self, order_id: str) -> None:
        with self._state_lock:
            if self._pending_order is not None and self._pending_order.broker_order_id == order_id:
                self._pending_order = None

    def _reconcile_pending_order(
        self,
        pending: _PendingOrder,
        risk: RiskController | None = None,
        notifier: ServerChanNotifier | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> None:
        now = time.monotonic()
        if now < pending.next_status_check_at:
            return
        updated_pending = _PendingOrder(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=pending.engine_snapshot,
            avg_price=pending.avg_price,
            next_status_check_at=now + self._order_status_poll_interval_seconds,
        )
        with self._state_lock:
            self._pending_order = updated_pending

        try:
            order_status = self._coerce_order_status(pending.broker.get_order_status(pending.broker_order_id), pending.broker_order_id)
        except Exception:
            logger.exception("failed to query pending order status for %s", pending.broker_order_id)
            return

        self._safe_update_order_status_from_result(order_status)
        status = order_status.status
        if status == "FILLED":
            self._finalize_pending_fill(updated_pending, order_status, risk=risk, notifier=notifier, notify_risk_event=notify_risk_event)
            self._clear_pending_order(updated_pending.broker_order_id)
            return
        if status in _FAILED_ORDER_STATUSES:
            fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
            if fill_qty > 0:
                self._finalize_pending_fill(updated_pending, order_status, risk=risk, notifier=notifier, fill_qty=fill_qty, notify_risk_event=notify_risk_event)
                self._clear_pending_order(updated_pending.broker_order_id)
                if self._should_restore_after_partial_terminal_fill(updated_pending, fill_qty) and restore_engine_snapshot is not None and updated_pending.engine_snapshot is not None:
                    restore_engine_snapshot(updated_pending.engine_snapshot)
                return
            self._pause_after_failed_order(updated_pending.broker_order_id, status, risk, notify_risk_event)
            self._clear_pending_order(updated_pending.broker_order_id)
            if restore_engine_snapshot is not None and updated_pending.engine_snapshot is not None:
                restore_engine_snapshot(updated_pending.engine_snapshot)
            return
        logger.debug("pending order still live: %s status=%s", updated_pending.broker_order_id, status)

    def _finalize_pending_fill(
        self,
        pending: _PendingOrder,
        order_status: OrderStatus,
        *,
        risk: RiskController | None = None,
        notifier: ServerChanNotifier | None = None,
        fill_qty: Decimal | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> None:
        fill_price = self._resolved_decimal(order_status, "executed_price", pending.price)
        fill_qty = fill_qty if fill_qty is not None else self._resolved_decimal(order_status, "executed_quantity", pending.quantity)
        if pending.action == "SELL":
            avg_price = pending.avg_price if pending.avg_price is not None else pending.price
            pnl = float((fill_price - avg_price) * fill_qty)
            self._safe_notify_order(notifier, "SELL", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            if risk is not None:
                risk.record_trade(pnl)
            logger.info("SELL filled: %s qty=%s price=%s pnl=%s", pending.symbol, fill_qty, fill_price, pnl)
            return
        if pending.action == "BUY_TO_COVER":
            avg_price = pending.avg_price if pending.avg_price is not None else pending.price
            pnl = float((avg_price - fill_price) * fill_qty)
            self._safe_notify_order(notifier, "BUY_TO_COVER", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            if risk is not None:
                risk.record_trade(pnl)
            logger.info("BUY_TO_COVER filled: %s qty=%s price=%s pnl=%s", pending.symbol, fill_qty, fill_price, pnl)
            return
        self._safe_notify_order(notifier, pending.action, pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
        logger.info("%s filled: %s qty=%s price=%s", pending.action, pending.symbol, fill_qty, fill_price)

    @staticmethod
    def _should_restore_after_partial_terminal_fill(pending: _PendingOrder, fill_qty: Decimal) -> bool:
        return pending.action in {"SELL", "BUY_TO_COVER"} and fill_qty < pending.quantity

    def _handle_terminal_fill_result(
        self,
        action: str,
        result: OrderResult,
        order_status: OrderStatus,
        broker: BrokerGateway,
        risk: RiskController | None,
        notifier: ServerChanNotifier | None,
        engine_snapshot: _EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> bool:
        status = order_status.status
        if status not in _FAILED_ORDER_STATUSES:
            return False
        fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
        if fill_qty <= 0:
            return False
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=result.broker_order_id,
            symbol=result.symbol,
            action=action,
            quantity=result.quantity,
            price=result.price,
            engine_snapshot=engine_snapshot,
            avg_price=avg_price,
        )
        self._finalize_pending_fill(pending, order_status, risk=risk, notifier=notifier, fill_qty=fill_qty, notify_risk_event=notify_risk_event)
        if engine_snapshot is not None and self._should_restore_after_partial_terminal_fill(pending, fill_qty) and restore_engine_snapshot is not None:
            restore_engine_snapshot(engine_snapshot)
        return True

    def _pause_after_failed_order(
        self,
        order_id: str,
        status: str,
        risk: RiskController | None = None,
        notify_risk_event: Callable[[str, str], None] | None = None,
    ) -> None:
        reason = f"order {order_id} ended with status {status}"
        if risk is not None:
            risk.pause(reason)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record order failure risk event for %s", order_id)
        if notify_risk_event is not None:
            try:
                notify_risk_event("ORDER_FAILED", reason)
            except Exception:
                logger.exception("failed to send order failure notification for %s", order_id)

    @staticmethod
    def _resolved_decimal(item: object, name: str, fallback: Decimal) -> Decimal:
        value = getattr(item, name, Decimal("0"))
        try:
            decimal_value = Decimal(str(value))
        except Exception:
            return fallback
        return decimal_value if decimal_value > 0 else fallback

    def _safe_record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        try:
            self._record_order(order_id, symbol, side, qty, price, status)
        except Exception:
            logger.exception("failed to record order %s for %s", order_id, symbol)

    def _safe_update_order_status(
        self,
        order_id: str,
        status: str,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
    ) -> None:
        try:
            self._update_order_status(order_id, status, filled_at, executed_quantity, executed_price)
        except Exception:
            logger.exception("failed to update order %s to status %s", order_id, status)

    def _safe_update_order_status_from_result(self, result: object) -> None:
        status = getattr(result, "status", "SUBMITTED")
        if status == "SUBMITTED":
            return
        filled_at = datetime.now(timezone.utc) if status in {"FILLED", "REJECTED", "CANCELLED"} else None
        self._safe_update_order_status(
            getattr(result, "broker_order_id", ""),
            status,
            filled_at,
            float(getattr(result, "executed_quantity", 0)) if getattr(result, "executed_quantity", 0) is not None else None,
            float(getattr(result, "executed_price", 0)) if getattr(result, "executed_price", 0) is not None else None,
        )

    def _wait_for_order_completion(self, result: OrderResult, broker: BrokerGateway | None = None) -> OrderStatus:
        broker = broker or getattr(result, "broker", None)
        status = getattr(result, "status", "SUBMITTED")
        last_status = OrderStatus(
            broker_order_id=result.broker_order_id,
            status=status,
            executed_quantity=getattr(result, "quantity", Decimal("0")) if status == "FILLED" else Decimal("0"),
            executed_price=getattr(result, "price", Decimal("0")) if status == "FILLED" else Decimal("0"),
        )
        if status in {"FILLED", "REJECTED", "CANCELLED"}:
            return last_status
        if broker is None:
            return last_status

        deadline = time.monotonic() + self._order_status_timeout_seconds
        while True:
            try:
                raw_status = broker.get_order_status(result.broker_order_id)
                last_status = self._coerce_order_status(raw_status, result.broker_order_id)
            except Exception:
                logger.exception("failed to query order status for %s", result.broker_order_id)
                return last_status
            if last_status.status in {"FILLED", "REJECTED", "CANCELLED"}:
                return last_status
            if time.monotonic() >= deadline:
                return last_status
            time.sleep(self._order_status_poll_interval_seconds)

    def _safe_notify_order(
        self,
        notifier: ServerChanNotifier | None,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> None:
        if notifier is None:
            return
        try:
            notifier.notify_order(side, symbol, quantity, price, order_id)
        except Exception:
            logger.exception("failed to send order notification for %s %s", side, symbol)

    @staticmethod
    def _coerce_order_status(result: object, default_order_id: str) -> OrderStatus:
        status = getattr(result, "status", "SUBMITTED")
        return OrderStatus(
            broker_order_id=str(getattr(result, "broker_order_id", default_order_id)),
            status=status,
            executed_quantity=TradeExecutionService._resolved_decimal(result, "executed_quantity", Decimal("0")),
            executed_price=TradeExecutionService._resolved_decimal(result, "executed_price", Decimal("0")),
        )
