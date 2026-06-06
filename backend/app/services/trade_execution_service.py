from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from threading import RLock
from typing import TYPE_CHECKING, Callable, Optional

from app.config import settings
from app.core.fees import estimate_round_trip_fee
from app.core.market_calendar import is_trading_hours

if TYPE_CHECKING:
    from app.core.audit import AuditLogger
    from app.core.broker import BrokerGateway, OrderResult, Quote
    from app.core.engine import EngineState
    from app.core.notifiers import NotifierInterface
    from app.core.risk import RiskController

logger = logging.getLogger("auto_trade.services.trade_execution_service")


class OrderPersistenceError(RuntimeError):
    """Raised when a broker order was submitted but could not be persisted locally."""


_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_FAILED_ORDER_STATUSES = {"REJECTED", "CANCELLED"}
_SKIPPED_ORDER_STATUS = "SKIPPED"
ENTRY_BUYING_POWER_USAGE = Decimal("0.9")
US_PRICE_TICK = Decimal("0.01")

# HKEX stepped tick table (https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism)
# Ordered ascending by upper bound; the matching tier is the first whose
# upper bound is strictly greater than the price.
_HK_TICK_TABLE: list[tuple[Decimal, Decimal]] = [
    (Decimal("0.25"), Decimal("0.001")),
    (Decimal("0.50"), Decimal("0.005")),
    (Decimal("10.00"), Decimal("0.010")),
    (Decimal("20.00"), Decimal("0.010")),
    (Decimal("50.00"), Decimal("0.020")),
    (Decimal("100.00"), Decimal("0.050")),
    (Decimal("200.00"), Decimal("0.100")),
    (Decimal("500.00"), Decimal("0.200")),
    (Decimal("1000.00"), Decimal("0.500")),
    (Decimal("2000.00"), Decimal("1.000")),
    (Decimal("5000.00"), Decimal("2.000")),
    (Decimal("9995.00"), Decimal("5.000")),
]


def _hk_tick_for(price: Decimal) -> Decimal:
    for upper, tick in _HK_TICK_TABLE:
        if price < upper:
            return tick
    return _HK_TICK_TABLE[-1][1]
_EngineSnapshot = tuple["EngineState", float, Optional[datetime]]
_NotifyRiskEvent = Callable[[str, str], object]
_RecordOrderSkipped = Callable[[str, str, str, dict[str, object]], None]


@dataclass(frozen=True)
class OrderStatus:
    broker_order_id: str
    status: str
    executed_quantity: Decimal = Decimal("0")
    executed_price: Decimal = Decimal("0")
    reason: str = ""


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
    submitted_at: float = 0.0


@dataclass
class _TrackedEntry:
    quantity: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")

    @property
    def avg_price(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return self.cost / self.quantity


_EntryPersistCallback = Callable[[str, Decimal, Decimal], None]


class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[[str, str, str, float, float, str], None],
        update_order_status: Callable[[str, str, datetime | None, float | None, float | None], None],
        record_risk_event: Callable[[str], None],
        record_order_skipped: _RecordOrderSkipped | None = None,
        persist_entry: _EntryPersistCallback | None = None,
        audit: AuditLogger | None = None,
        margin_safety_factor: float | None = None,
    ) -> None:
        self._record_order = record_order
        self._update_order_status = update_order_status
        self._record_risk_event = record_risk_event
        self._record_order_skipped = record_order_skipped
        self._persist_entry = persist_entry
        self._audit = audit
        self.margin_safety_factor = margin_safety_factor
        self._state_lock = RLock()
        self._pending_orders: dict[str, _PendingOrder] = {}
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0
        self._entry_positions: dict[str, _TrackedEntry] = {}

    def load_tracked_entries(self, entries: dict[str, tuple[Decimal, Decimal]]) -> None:
        """Restore tracked entry positions (typically at runner startup)."""
        with self._state_lock:
            self._entry_positions.clear()
            for symbol, (quantity, cost) in entries.items():
                if quantity <= 0 or cost <= 0:
                    continue
                self._entry_positions[symbol] = _TrackedEntry(quantity=quantity, cost=cost)

    def load_pending_orders(self, pending_orders: list[_PendingOrder]) -> None:
        with self._state_lock:
            existing_by_id = {
                pending.broker_order_id: pending
                for pending in self._pending_orders.values()
            }
            self._pending_orders.clear()
            for pending in pending_orders:
                existing = existing_by_id.get(pending.broker_order_id)
                if existing is not None:
                    pending = _PendingOrder(
                        broker=pending.broker,
                        broker_order_id=pending.broker_order_id,
                        symbol=pending.symbol,
                        action=pending.action,
                        quantity=pending.quantity,
                        price=pending.price,
                        engine_snapshot=existing.engine_snapshot,
                        avg_price=existing.avg_price if existing.avg_price is not None else pending.avg_price,
                        next_status_check_at=existing.next_status_check_at,
                        submitted_at=existing.submitted_at,
                    )
                self._pending_orders[pending.symbol] = pending

    def snapshot_tracked_entries(self) -> dict[str, tuple[Decimal, Decimal]]:
        with self._state_lock:
            return {
                symbol: (entry.quantity, entry.cost)
                for symbol, entry in self._entry_positions.items()
            }

    @property
    def has_pending_order(self) -> bool:
        with self._state_lock:
            return bool(self._pending_orders)

    @property
    def pending_order(self) -> _PendingOrder | None:
        with self._state_lock:
            return next(iter(self._pending_orders.values()), None)

    def pending_order_for(self, symbol: str) -> _PendingOrder | None:
        with self._state_lock:
            return self._pending_orders.get(symbol)

    @property
    def _pending_order(self) -> _PendingOrder | None:
        return self.pending_order

    @_pending_order.setter
    def _pending_order(self, pending: _PendingOrder | None) -> None:
        with self._state_lock:
            if pending is None:
                self._pending_orders.clear()
                return
            self._pending_orders[pending.symbol] = pending

    def reconcile(
        self,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        with self._state_lock:
            pending_orders = list(self._pending_orders.values())
        for pending in pending_orders:
            self._reconcile_pending_order(
                pending,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )

    def cancel_pending_order(
        self,
        *,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = next(iter(self._pending_orders.values()), None)
        if pending is None:
            return OrderStatus("", "NO_PENDING_ORDER")
        return self.cancel_pending_order_for_symbol(
            pending.symbol,
            restore_engine_snapshot=restore_engine_snapshot,
        )

    def cancel_pending_order_for_symbol(
        self,
        symbol: str,
        *,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = self._pending_orders.get(symbol)
        if pending is None:
            return OrderStatus("", "NO_PENDING_ORDER")

        try:
            order_status = self._coerce_order_status(
                pending.broker.cancel_order(pending.broker_order_id),
                pending.broker_order_id,
            )
        except Exception:
            logger.exception("failed to cancel pending order %s", pending.broker_order_id)
            return OrderStatus(pending.broker_order_id, "CANCEL_FAILED")

        self._safe_update_order_status_from_result(order_status)
        self._clear_pending_order(pending.broker_order_id)
        if restore_engine_snapshot is not None and pending.engine_snapshot is not None:
            restore_engine_snapshot(pending.engine_snapshot)
        logger.info("pending order cancelled: %s status=%s", pending.broker_order_id, order_status.status)
        return order_status

    def cancel_order_by_id(
        self,
        order_id: str,
        broker: BrokerGateway,
        *,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = next(
                (item for item in self._pending_orders.values() if item.broker_order_id == order_id),
                None,
            )
        if pending is not None:
            return self.cancel_pending_order_for_symbol(
                pending.symbol,
                restore_engine_snapshot=restore_engine_snapshot,
            )

        try:
            order_status = self._coerce_order_status(broker.cancel_order(order_id), order_id)
        except Exception:
            logger.exception("failed to cancel order %s", order_id)
            return OrderStatus(order_id, "CANCEL_FAILED")
        self._safe_update_order_status_from_result(order_status)
        logger.info("order cancelled by id: %s status=%s", order_id, order_status.status)
        return order_status

    def execute(
        self,
        action: str,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        cash_currency: str,
        *,
        market: str = "US",
        trading_session_mode: str = "ANY",
        min_profit_amount: Decimal | float | int = Decimal("0"),
        allow_loss_exit: bool = False,
        fee_rate: Decimal | float | int = Decimal("0"),
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus | None:
        if trading_session_mode == "RTH_ONLY" and not is_trading_hours(market):
            # SESSION skip records ORDER_SKIPPED only; TRADING_SESSION_BLOCKED is layer A.
            return self._skip_order(
                symbol,
                action,
                f"non-RTH for {market}",
                skip_category="SESSION",
            )

        risk_result = risk.check()
        if not risk_result.approved:
            logger.warning("execute rejected by risk: %s", risk_result.reason)
            return self._skip_order(symbol, action, risk_result.reason, skip_category="RISK")

        with self._state_lock:
            pending = self._pending_orders.get(symbol)
            if pending is not None:
                logger.warning("execute skipped: pending order %s still live for %s", pending.broker_order_id, symbol)
                return self._skip_order(symbol, action, "pending order in flight", skip_category="PENDING")

        if action == "BUY":
            return self._execute_buy(symbol, quote, broker, risk, notifier, cash_currency, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "SELL":
            return self._execute_sell(symbol, quote, broker, risk, notifier, min_profit_amount=min_profit_amount, allow_loss_exit=allow_loss_exit, fee_rate=fee_rate, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "SELL_SHORT":
            return self._execute_sell_short(symbol, quote, broker, risk, notifier, cash_currency, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        if action == "BUY_TO_COVER":
            return self._execute_buy_to_cover(symbol, quote, broker, risk, notifier, min_profit_amount=min_profit_amount, allow_loss_exit=allow_loss_exit, fee_rate=fee_rate, engine_snapshot=engine_snapshot, restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event)
        logger.warning("unknown action: %s", action)
        return None

    def _entry_quantity_from_margin_power(
        self,
        broker: BrokerGateway,
        symbol: str,
        side: str,
        price: Decimal,
        cash_currency: str,
        *,
        safety_factor: float | None = None,
    ) -> int:
        max_qty = broker.estimate_margin_max_quantity(symbol, side, price, cash_currency)
        if safety_factor is not None:
            factor = Decimal(str(safety_factor))
        elif self.margin_safety_factor is not None:
            factor = Decimal(str(self.margin_safety_factor))
        else:
            factor = ENTRY_BUYING_POWER_USAGE
        qty = int(max_qty * factor)
        if qty <= 0:
            logger.warning(
                "%s: qty <= 0, margin_max_qty=%s price=%s currency=%s factor=%s",
                side,
                max_qty,
                price,
                cash_currency,
                factor,
            )
        return qty

    @staticmethod
    def _normalize_limit_price(symbol: str, side: str, price: Decimal) -> Decimal:
        upper_symbol = symbol.upper()
        rounding = ROUND_FLOOR if side in {"BUY", "BUY_TO_COVER"} else ROUND_CEILING
        if upper_symbol.endswith(".US"):
            return price.quantize(US_PRICE_TICK, rounding=rounding)
        if upper_symbol.endswith(".HK"):
            if price <= 0:
                return price
            tick = _hk_tick_for(price)
            steps = (price / tick).to_integral_value(rounding=rounding)
            return (steps * tick).quantize(tick)
        return price

    @staticmethod
    def _coerce_non_negative_decimal(value: Decimal | float | int) -> Decimal:
        try:
            amount = Decimal(str(value))
        except Exception:
            return Decimal("0")
        return amount if amount > 0 else Decimal("0")

    @staticmethod
    def _minimum_required_profit_amount(
        avg_price: Decimal,
        quantity: Decimal,
        min_profit_amount: Decimal | float | int,
    ) -> Decimal:
        buffer_pct = Decimal(str(settings.min_exit_profit_pct)) / Decimal("100")
        pct_profit_amount = avg_price * quantity * buffer_pct
        configured_amount = TradeExecutionService._coerce_non_negative_decimal(min_profit_amount)
        return max(pct_profit_amount, configured_amount)

    def _profit_guard_for_exit(
        self,
        *,
        action: str,
        symbol: str,
        avg_price: Decimal,
        exit_price: Decimal,
        quantity: Decimal,
        min_profit_amount: Decimal | float | int,
        allow_loss_exit: bool,
        fee_rate: Decimal | float | int = Decimal("0"),
    ) -> OrderStatus | None:
        if allow_loss_exit or quantity <= 0 or avg_price <= 0:
            return None
        expected_profit = (
            (exit_price - avg_price) * quantity
            if action == "SELL"
            else (avg_price - exit_price) * quantity
        )
        required_profit = self._minimum_required_profit_amount(avg_price, quantity, min_profit_amount)
        rate = self._coerce_non_negative_decimal(fee_rate)
        estimated_fees = estimate_round_trip_fee(
            entry_price=avg_price,
            exit_price=exit_price,
            quantity=quantity,
            one_side_rate=rate,
        )
        net_expected_profit = expected_profit - estimated_fees
        if net_expected_profit >= required_profit:
            return None
        return self._skip_order(
            symbol,
            action,
            (
                f"net expected profit {net_expected_profit:.2f} after estimated fees "
                f"{estimated_fees:.2f} is below required minimum profit {required_profit:.2f}"
            ),
            skip_category="FEE",
            expected_profit=float(expected_profit),
            estimated_fees=float(estimated_fees),
            net_expected_profit=float(net_expected_profit),
            required_profit=float(required_profit),
            quantity=float(quantity),
            price=float(exit_price),
        )

    def _skip_order(
        self,
        symbol: str,
        action: str,
        reason: str,
        *,
        skip_category: str = "",
        **payload: object,
    ) -> OrderStatus:
        logger.info("%s skipped for %s: %s", action, symbol, reason)
        if self._record_order_skipped is not None:
            try:
                full_payload: dict[str, object] = {"skip_category": skip_category, **payload}
                self._record_order_skipped(symbol, action, reason, full_payload)
            except Exception:
                logger.exception("failed to record skipped order event for %s %s", action, symbol)
        return OrderStatus("", _SKIPPED_ORDER_STATUS, reason=reason)

    @staticmethod
    def _exit_quantity_from_position(position: object) -> Decimal:
        try:
            position_quantity = Decimal(str(getattr(position, "quantity", Decimal("0"))))
        except Exception:
            return Decimal("0")
        if position_quantity <= 0:
            return Decimal("0")

        available = getattr(position, "available_quantity", None)
        if available is None:
            return position_quantity
        try:
            available_quantity = Decimal(str(available))
        except Exception:
            return position_quantity
        if available_quantity <= 0:
            return Decimal("0")
        return min(position_quantity, available_quantity)

    def _execute_buy(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        cash_currency: str,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "BUY", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return None
        qty = self._entry_quantity_from_margin_power(broker, symbol, "BUY", price, cash_currency)
        if qty <= 0:
            return None

        order_status = self._submit_limit_order(
            "BUY",
            symbol,
            "BUY",
            Decimal(qty),
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
        )
        if order_status is None or order_status.status != "FILLED":
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._record_entry_price(symbol, fill_price, fill_qty)
        self._safe_notify_order(notifier, "BUY", symbol, str(fill_qty), str(fill_price), order_status.broker_order_id)
        logger.info("BUY: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        return order_status

    def _execute_sell(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        *,
        min_profit_amount: Decimal | float | int = Decimal("0"),
        allow_loss_exit: bool = False,
        fee_rate: Decimal | float | int = Decimal("0"),
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return None
        qty = self._exit_quantity_from_position(long_pos)
        if qty <= 0:
            logger.warning("SELL: no available long quantity for %s", symbol)
            return self._skip_order(symbol, "SELL", f"no available long quantity for {symbol}", skip_category="POSITION")

        price = self._normalize_limit_price(symbol, "SELL", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return None
        pos_avg_price = self._resolve_avg_price_for_exit(symbol, long_pos.avg_price, qty)
        profit_guard = self._profit_guard_for_exit(
            action="SELL",
            symbol=symbol,
            avg_price=pos_avg_price,
            exit_price=price,
            quantity=qty,
            min_profit_amount=min_profit_amount,
            allow_loss_exit=allow_loss_exit,
            fee_rate=fee_rate,
        )
        if profit_guard is not None:
            return profit_guard

        order_status = self._submit_limit_order(
            "SELL",
            symbol,
            "SELL",
            qty,
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            avg_price=pos_avg_price,
        )
        if order_status is None or order_status.status != "FILLED":
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else qty
        pnl = float((fill_price - pos_avg_price) * fill_qty)
        self._safe_notify_order(notifier, "SELL", symbol, str(fill_qty), str(fill_price), order_status.broker_order_id)
        risk.record_trade(pnl)
        self._consume_entry_quantity(symbol, fill_qty)
        logger.info("SELL: %s qty=%s price=%s avg_price=%s pnl=%s", symbol, fill_qty, fill_price, pos_avg_price, pnl)
        return order_status

    def _execute_sell_short(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        cash_currency: str,
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "SELL_SHORT", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return None

        qty = self._entry_quantity_from_margin_power(broker, symbol, "SELL", price, cash_currency)
        if qty <= 0:
            return None

        order_status = self._submit_limit_order(
            "SELL_SHORT",
            symbol,
            "SELL",
            Decimal(qty),
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
        )
        if order_status is None or order_status.status != "FILLED":
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._record_entry_price(symbol, fill_price, fill_qty)
        self._safe_notify_order(notifier, "SELL_SHORT", symbol, str(fill_qty), str(fill_price), order_status.broker_order_id)
        logger.info("SELL_SHORT: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        return order_status

    def _execute_buy_to_cover(
        self,
        symbol: str,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        *,
        min_profit_amount: Decimal | float | int = Decimal("0"),
        allow_loss_exit: bool = False,
        fee_rate: Decimal | float | int = Decimal("0"),
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus | None:
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return None
        qty = self._exit_quantity_from_position(pos)
        if qty <= 0:
            logger.warning("BUY_TO_COVER: no available short quantity for %s", symbol)
            return self._skip_order(symbol, "BUY_TO_COVER", f"no available short quantity for {symbol}", skip_category="POSITION")

        price = self._normalize_limit_price(symbol, "BUY_TO_COVER", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return None
        pos_avg_price = self._resolve_avg_price_for_exit(symbol, pos.avg_price, qty)
        profit_guard = self._profit_guard_for_exit(
            action="BUY_TO_COVER",
            symbol=symbol,
            avg_price=pos_avg_price,
            exit_price=price,
            quantity=qty,
            min_profit_amount=min_profit_amount,
            allow_loss_exit=allow_loss_exit,
            fee_rate=fee_rate,
        )
        if profit_guard is not None:
            return profit_guard

        order_status = self._submit_limit_order(
            "BUY_TO_COVER",
            symbol,
            "BUY",
            qty,
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            avg_price=pos_avg_price,
        )
        if order_status is None or order_status.status != "FILLED":
            return order_status

        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else qty
        pnl = float((pos_avg_price - fill_price) * fill_qty)
        self._safe_notify_order(notifier, "BUY_TO_COVER", symbol, str(fill_qty), str(fill_price), order_status.broker_order_id)
        risk.record_trade(pnl)
        self._consume_entry_quantity(symbol, fill_qty)
        logger.info("BUY_TO_COVER: %s qty=%s price=%s avg_price=%s pnl=%s", symbol, fill_qty, fill_price, pos_avg_price, pnl)
        return order_status

    def _submit_limit_order(
        self,
        action: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        *,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        avg_price: Decimal | None = None,
    ) -> OrderStatus | None:
        """Submit a limit order, persist it, and handle immediate live/terminal/filled outcomes.

        Returns ``OrderStatus`` for live, terminal, or filled orders.  Callers that
        need post-fill bookkeeping (entry recording, PnL, position consumption)
        should inspect ``status == "FILLED"`` and perform their own tail logic.
        """
        result = broker.submit_limit_order(symbol, side, qty, price)
        status = getattr(result, "status", "SUBMITTED")
        try:
            self._persist_submitted_order(
                result.broker_order_id, symbol, action, float(qty), float(price), status
            )
        except OrderPersistenceError:
            return self._recover_from_missing_order_record(
                result,
                broker,
                risk,
                notify_risk_event=notify_risk_event,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
            )
        order_status = self._order_status_from_submit_result(result)
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            self._track_pending_order(action, result, broker, engine_snapshot, avg_price=avg_price)
            logger.info("%s pending: %s status=%s", action, result.broker_order_id, order_status.status)
            return order_status

        if self._handle_terminal_fill_result(
            action,
            result,
            order_status,
            broker,
            risk,
            notifier,
            engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            avg_price=avg_price,
            notify_risk_event=notify_risk_event,
        ):
            return order_status

        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status, risk, notify_risk_event)
            logger.warning("%s not filled: %s status=%s", action, result.broker_order_id, order_status.status)
            return order_status

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
            submitted_at=time.monotonic(),
        )
        with self._state_lock:
            self._pending_orders[pending.symbol] = pending

    def _clear_pending_order(self, order_id: str) -> None:
        with self._state_lock:
            for symbol, pending in list(self._pending_orders.items()):
                if pending.broker_order_id == order_id:
                    del self._pending_orders[symbol]
                    return

    def _reconcile_pending_order(
        self,
        pending: _PendingOrder,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        now = time.monotonic()
        if now < pending.next_status_check_at:
            return
        if (
            self._order_status_timeout_seconds > 0
            and now - pending.submitted_at >= self._order_status_timeout_seconds
        ):
            self._handle_pending_order_timeout(
                pending,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )
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
            submitted_at=pending.submitted_at,
        )
        with self._state_lock:
            self._pending_orders[updated_pending.symbol] = updated_pending

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

    def _handle_pending_order_timeout(
        self,
        pending: _PendingOrder,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        reason = f"pending order {pending.broker_order_id} timed out after {self._order_status_timeout_seconds:.0f}s"
        logger.warning(reason)
        try:
            order_status = self._coerce_order_status(
                pending.broker.get_order_status(pending.broker_order_id),
                pending.broker_order_id,
            )
            self._safe_update_order_status_from_result(order_status)
            if order_status.status == "FILLED":
                self._finalize_pending_fill(pending, order_status, risk=risk, notifier=notifier, notify_risk_event=notify_risk_event)
                self._clear_pending_order(pending.broker_order_id)
                return
            if order_status.status in _FAILED_ORDER_STATUSES:
                fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
                if fill_qty > 0:
                    self._finalize_pending_fill(
                        pending,
                        order_status,
                        risk=risk,
                        notifier=notifier,
                        fill_qty=fill_qty,
                        notify_risk_event=notify_risk_event,
                    )
                else:
                    self._pause_after_failed_order(pending.broker_order_id, order_status.status, risk, notify_risk_event)
                self._clear_pending_order(pending.broker_order_id)
                if restore_engine_snapshot is not None and pending.engine_snapshot is not None:
                    restore_engine_snapshot(pending.engine_snapshot)
                return
        except Exception:
            logger.exception("failed to query pending order status during timeout for %s", pending.broker_order_id)

        if risk is not None:
            risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record pending-order timeout risk event for %s", pending.broker_order_id)
        if notify_risk_event is not None:
            try:
                notify_risk_event("ORDER_TIMEOUT", reason)
            except Exception:
                logger.exception("failed to send pending-order timeout notification for %s", pending.broker_order_id)

        self._clear_pending_order(pending.broker_order_id)
        if restore_engine_snapshot is not None and pending.engine_snapshot is not None:
            restore_engine_snapshot(pending.engine_snapshot)

    def _finalize_pending_fill(
        self,
        pending: _PendingOrder,
        order_status: OrderStatus,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        fill_qty: Decimal | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        fill_price = self._resolved_decimal(order_status, "executed_price", pending.price)
        fill_qty = fill_qty if fill_qty is not None else self._resolved_decimal(order_status, "executed_quantity", pending.quantity)
        if pending.action == "SELL":
            avg_price = self._resolve_avg_price_for_exit(pending.symbol, pending.avg_price if pending.avg_price is not None and pending.avg_price > 0 else None, fill_qty)
            pnl = float((fill_price - avg_price) * fill_qty)
            self._safe_notify_order(notifier, "SELL", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            if risk is not None:
                risk.record_trade(pnl)
            self._consume_entry_quantity(pending.symbol, fill_qty)
            logger.info("SELL filled: %s qty=%s price=%s avg_price=%s pnl=%s", pending.symbol, fill_qty, fill_price, avg_price, pnl)
            return
        if pending.action == "BUY_TO_COVER":
            avg_price = self._resolve_avg_price_for_exit(pending.symbol, pending.avg_price if pending.avg_price is not None and pending.avg_price > 0 else None, fill_qty)
            pnl = float((avg_price - fill_price) * fill_qty)
            self._safe_notify_order(notifier, "BUY_TO_COVER", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            if risk is not None:
                risk.record_trade(pnl)
            self._consume_entry_quantity(pending.symbol, fill_qty)
            logger.info("BUY_TO_COVER filled: %s qty=%s price=%s avg_price=%s pnl=%s", pending.symbol, fill_qty, fill_price, avg_price, pnl)
            return
        self._safe_notify_order(notifier, pending.action, pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
        if pending.action in {"BUY", "SELL_SHORT"}:
            self._record_entry_price(pending.symbol, fill_price, fill_qty)
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
        notifier: "NotifierInterface | None",
        engine_snapshot: _EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
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
        notify_risk_event: _NotifyRiskEvent | None = None,
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

    def _persist_submitted_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        try:
            self._record_order(order_id, symbol, side, qty, price, status)
        except Exception as exc:
            logger.exception("failed to record order %s for %s", order_id, symbol)
            raise OrderPersistenceError(f"failed to persist order {order_id}") from exc

    def _recover_from_missing_order_record(
        self,
        result: OrderResult,
        broker: BrokerGateway,
        risk: RiskController,
        *,
        notify_risk_event: _NotifyRiskEvent | None = None,
        engine_snapshot: _EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[_EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        reason = f"order {result.broker_order_id} submitted but local record failed"
        logger.error(reason)
        if self._order_status_is_live(result):
            try:
                broker.cancel_order(result.broker_order_id)
            except Exception:
                logger.exception("failed to cancel orphan order %s after persistence failure", result.broker_order_id)
        risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record orphan-order risk event for %s", result.broker_order_id)
        if notify_risk_event is not None:
            try:
                notify_risk_event("ORDER_PERSISTENCE_FAILED", reason)
            except Exception:
                logger.exception("failed to send orphan-order notification for %s", result.broker_order_id)
        if restore_engine_snapshot is not None and engine_snapshot is not None:
            restore_engine_snapshot(engine_snapshot)
        return OrderStatus(result.broker_order_id, "REJECTED", reason=reason)

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
        filled_at = datetime.now(timezone.utc) if status == "FILLED" else None
        self._safe_update_order_status(
            getattr(result, "broker_order_id", ""),
            status,
            filled_at,
            float(getattr(result, "executed_quantity", 0)) if getattr(result, "executed_quantity", 0) is not None else None,
            float(getattr(result, "executed_price", 0)) if getattr(result, "executed_price", 0) is not None else None,
        )

    @staticmethod
    def _order_status_from_submit_result(result: OrderResult) -> OrderStatus:
        status = getattr(result, "status", "SUBMITTED")
        return OrderStatus(
            broker_order_id=result.broker_order_id,
            status=status,
            executed_quantity=getattr(result, "executed_quantity", getattr(result, "quantity", Decimal("0"))) if status == "FILLED" else Decimal("0"),
            executed_price=getattr(result, "executed_price", getattr(result, "price", Decimal("0"))) if status == "FILLED" else Decimal("0"),
        )

    def _safe_notify_order(
        self,
        notifier: "NotifierInterface | None",
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

    def _record_entry_price(self, symbol: str, fill_price: Decimal, fill_qty: Decimal) -> None:
        if fill_price <= 0 or fill_qty <= 0:
            return
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            if entry is None:
                entry = _TrackedEntry()
                self._entry_positions[symbol] = entry
            previous_avg = entry.avg_price
            entry.quantity += fill_qty
            entry.cost += fill_price * fill_qty
            snapshot = (entry.quantity, entry.cost)
            if previous_avg <= 0:
                logger.info("entry price recorded for %s: avg=%s qty=%s", symbol, entry.avg_price, entry.quantity)
            else:
                logger.info(
                    "entry price updated for %s: avg=%s -> %s qty=%s",
                    symbol,
                    previous_avg,
                    entry.avg_price,
                    entry.quantity,
                )
        self._persist_entry_safe(symbol, snapshot[0], snapshot[1])

    def _persist_entry_safe(self, symbol: str, quantity: Decimal, cost: Decimal) -> None:
        if self._persist_entry is None:
            return
        try:
            self._persist_entry(symbol, quantity, cost)
        except Exception:
            logger.exception("failed to persist tracked entry for %s", symbol)

    def _resolve_avg_price_for_exit(self, symbol: str, broker_avg_price: Decimal | None, exit_qty: Decimal) -> Decimal:
        with self._state_lock:
            tracked_entry = self._entry_positions.get(symbol)
            tracked_qty = tracked_entry.quantity if tracked_entry is not None else Decimal("0")
            tracked_avg = tracked_entry.avg_price if tracked_entry is not None else Decimal("0")

        tracked_covers_exit = tracked_qty >= exit_qty > 0
        if tracked_avg > 0 and tracked_covers_exit:
            if broker_avg_price is not None and broker_avg_price > 0 and abs(tracked_avg - broker_avg_price) / broker_avg_price > Decimal("0.02"):
                # Divergence exceeds 2% tolerance: prefer tracked weighted entry for accurate pnl
                logger.warning(
                    "avg_price mismatch for %s: tracked=%s vs broker=%s, using tracked weighted entry price for accurate pnl",
                    symbol,
                    tracked_avg,
                    broker_avg_price,
                )
                return tracked_avg
            if broker_avg_price is not None and broker_avg_price > 0:
                # Divergence within tolerance (<=2%) or no divergence: use broker price
                if tracked_avg != broker_avg_price:
                    logger.debug(
                        "avg_price within-tolerance divergence for %s: tracked=%s vs broker=%s (diff=%s), using broker price",
                        symbol,
                        tracked_avg,
                        broker_avg_price,
                        abs(tracked_avg - broker_avg_price),
                    )
                return broker_avg_price
            # Broker price unavailable; fall back to tracked weighted entry
            return tracked_avg

        if broker_avg_price is not None and broker_avg_price > 0:
            return broker_avg_price

        if tracked_avg > 0:
            logger.warning(
                "tracked entry quantity for %s (%s) is below exit quantity %s; using tracked avg as fallback",
                symbol,
                tracked_qty,
                exit_qty,
            )
            return tracked_avg

        logger.warning("no avg_price available for %s exit, pnl may be zero", symbol)
        return Decimal("0")

    def _consume_entry_quantity(self, symbol: str, fill_qty: Decimal) -> None:
        if fill_qty <= 0:
            return
        snapshot: tuple[Decimal, Decimal] | None = None
        cleared = False
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            if entry is None or entry.quantity <= 0:
                return
            consumed = min(fill_qty, entry.quantity)
            avg_price = entry.avg_price
            entry.quantity -= consumed
            entry.cost -= avg_price * consumed
            if entry.quantity <= 0:
                self._entry_positions.pop(symbol, None)
                cleared = True
            else:
                if entry.cost < 0:
                    entry.cost = Decimal("0")
                snapshot = (entry.quantity, entry.cost)
        if cleared:
            self._persist_entry_safe(symbol, Decimal("0"), Decimal("0"))
        elif snapshot is not None:
            self._persist_entry_safe(symbol, snapshot[0], snapshot[1])

    def clear_entry_price(self, symbol: str) -> None:
        with self._state_lock:
            self._entry_positions.pop(symbol, None)
        self._persist_entry_safe(symbol, Decimal("0"), Decimal("0"))
