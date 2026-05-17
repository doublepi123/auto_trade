from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerGateway, OrderResult, Quote
from app.core.engine import StrategyEngine, StrategyParams, TriggerResult, EngineState
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, RiskEvent
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.strategy_service import StrategyService
from app.api.ws import manager

logger = logging.getLogger("auto_trade.runner")

_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_FAILED_ORDER_STATUSES = {"REJECTED", "CANCELLED"}
_EngineSnapshot = tuple[EngineState, float, Optional[datetime]]


@dataclass
class _PendingOrder:
    broker: BrokerGateway
    broker_order_id: str
    symbol: str
    action: str
    quantity: Decimal
    price: Decimal
    engine_snapshot: _EngineSnapshot
    avg_price: Decimal | None = None
    next_status_check_at: float = 0.0


@dataclass
class _TriggerContext:
    broker: BrokerGateway
    symbol: str
    cash_currency: str


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._quotes_subscribed = False
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0
        self._trigger_in_flight = False
        self._pending_order: _PendingOrder | None = None

    def _initialize_runner(self) -> None:
        db = SessionLocal()
        try:
            svc = StrategyService(db)
            config = svc.get_config()
            state = svc.get_runtime_state()

            self.engine.params = StrategyParams(
                symbol=config.symbol,
                market=config.market,
                buy_low=config.buy_low,
                sell_high=config.sell_high,
                short_selling=config.short_selling,
            )
            try:
                self.engine.state = EngineState(state.engine_state)
            except ValueError:
                logger.warning("invalid engine state %r in DB, defaulting to FLAT", state.engine_state)
                self.engine.state = EngineState.FLAT
            self.engine.last_price = state.last_price
            self.engine.last_trigger_price = state.last_trigger_price
            self.engine.last_trigger_at = state.last_trigger_at

            self.risk.config = RiskConfig(
                max_daily_loss=config.max_daily_loss,
                max_consecutive_losses=config.max_consecutive_losses,
            )
            self.risk.daily_pnl = state.daily_pnl
            self.risk.consecutive_losses = state.consecutive_losses
            self.risk.kill_switch = state.kill_switch
            self.risk.paused = state.paused
            self._pause_if_unresolved_live_order_exists(db)

            self._apply_credentials(self._load_credentials(), resubscribe=False)

            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                pass

            if config.symbol and not self._quotes_subscribed:
                try:
                    self.broker.subscribe_quotes(config.symbol, self._on_quote)
                    self._quotes_subscribed = True
                    logger.info(f"subscribed to {config.symbol} quotes")
                except Exception as exc:
                    logger.error(f"quote subscription failed for {config.symbol}: {exc}")
                    logger.error("system running without quote updates - trading engine may be non-functional")
        finally:
            db.close()

    def _pause_if_unresolved_live_order_exists(self, db: Session) -> None:
        order = (
            db.query(OrderRecord)
            .filter(OrderRecord.status.in_(_LIVE_ORDER_STATUSES))
            .order_by(OrderRecord.id.desc())
            .first()
        )
        if order is None:
            return
        reason = f"unresolved live order {order.broker_order_id} requires manual confirmation"
        logger.warning(reason)
        self.risk.pause(reason)

    def start(self) -> bool:
        with self._start_lock:
            if self._running:
                return False
            if self._thread is not None and self._thread.is_alive():
                self._running = False
                self._thread.join(timeout=10)
            try:
                self._initialize_runner()
            except Exception:
                logger.exception("runner initialization failed")
                return False
            self._running = True

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("runner started")
        return True

    def _load_credentials(self) -> PlainCredentials:
        db = SessionLocal()
        try:
            return CredentialsService(db).get_plain_credentials()
        finally:
            db.close()

    def _apply_credentials(self, credentials: PlainCredentials, *, resubscribe: bool) -> None:
        with self._state_lock:
            if resubscribe and (self._trigger_in_flight or self._pending_order is not None):
                logger.warning("credential reload skipped while order execution is in flight")
                return
            symbol = self.engine.params.symbol
            should_resubscribe = resubscribe and bool(symbol)
            new_notifier = ServerChanNotifier(credentials.sct_key if credentials.sct_key else settings.sct_key)

            self._set_or_clear_env("LONGPORT_APP_KEY", credentials.longbridge_app_key or settings.longbridge_app_key)
            self._set_or_clear_env("LONGPORT_APP_SECRET", credentials.longbridge_app_secret or settings.longbridge_app_secret)
            self._set_or_clear_env(
                "LONGPORT_ACCESS_TOKEN",
                credentials.longbridge_access_token or settings.longbridge_access_token,
            )

            new_broker = BrokerGateway()

            if should_resubscribe:
                try:
                    new_broker.subscribe_quotes(symbol, self._on_quote)
                except Exception as exc:
                    logger.warning(f"cannot subscribe quotes after credential reload: {exc}")
                    new_broker.close()
                    return

            old_broker = self.broker
            old_broker.close()
            self.broker = new_broker
            self.notifier = new_notifier

            if should_resubscribe:
                self._quotes_subscribed = True
            else:
                self._quotes_subscribed = False

    def reload_credentials(self) -> None:
        self._apply_credentials(self._load_credentials(), resubscribe=self._running)

    @staticmethod
    def _set_or_clear_env(name: str, value: str) -> None:
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    def reload_strategy(self) -> None:
        with self._state_lock:
            if self._trigger_in_flight or self._pending_order is not None:
                logger.warning("strategy reload skipped while order execution is in flight")
                return
            db = SessionLocal()
            try:
                svc = StrategyService(db)
                config = svc.get_config()
                old_symbol = self.engine.params.symbol
                self.engine.params = StrategyParams(
                    symbol=config.symbol,
                    market=config.market,
                    buy_low=config.buy_low,
                    sell_high=config.sell_high,
                    short_selling=config.short_selling,
                )
                self.risk.config = RiskConfig(
                    max_daily_loss=config.max_daily_loss,
                    max_consecutive_losses=config.max_consecutive_losses,
                )
                if config.symbol != old_symbol and self._running:
                    if self._quotes_subscribed:
                        try:
                            self.broker.unsubscribe_quotes()
                        except Exception:
                            logger.warning("failed to unsubscribe old symbol during strategy reload")
                        self._quotes_subscribed = False
                    if config.symbol:
                        try:
                            self.broker.subscribe_quotes(config.symbol, self._on_quote)
                            self._quotes_subscribed = True
                            logger.info(f"re-subscribed to {config.symbol} after strategy reload")
                        except Exception as exc:
                            logger.error(f"quote subscription failed after strategy reload: {exc}")
            finally:
                db.close()

    def stop(self) -> None:
        broker_to_close: object | None = None
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
            if not self._trigger_in_flight and self._pending_order is None:
                broker_to_close = self.broker
        if broker_to_close is not None:
            broker_to_close.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _on_quote(self, quote: Quote) -> None:
        pending_order: _PendingOrder | None = None
        result: TriggerResult | None = None
        engine_snapshot: _EngineSnapshot | None = None
        trigger_context: _TriggerContext | None = None
        processing_started = False
        broker_to_close: object | None = None

        try:
            with self._state_lock:
                if not self._running or self._trigger_in_flight:
                    return
                if self._pending_order is not None:
                    pending_order = self._pending_order
                    self._trigger_in_flight = True
                    processing_started = True
                else:
                    engine_snapshot = self._engine_snapshot()
                    trigger_context = _TriggerContext(
                        broker=self.broker,
                        symbol=self.engine.params.symbol,
                        cash_currency=self._cash_currency(),
                    )
                    result = self.engine.update_price(quote.last_price)
                    if result.triggered:
                        self._trigger_in_flight = True
                        processing_started = True

            if pending_order is not None:
                self._reconcile_pending_order(pending_order)
                return

            self._broadcast_status()
            if result is None or not result.triggered or engine_snapshot is None or trigger_context is None:
                return

            try:
                executed = self._handle_trigger(result, quote, engine_snapshot, trigger_context)
                if not executed:
                    with self._state_lock:
                        self._restore_engine_snapshot(engine_snapshot)
                    self._broadcast_status()
                else:
                    self._broadcast_status()
            except Exception:
                with self._state_lock:
                    self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
                raise
        except Exception:
            logger.exception("error processing quote")
        finally:
            if processing_started:
                with self._state_lock:
                    self._trigger_in_flight = False
                    if not self._running and self._pending_order is None:
                        broker_to_close = self.broker
        if broker_to_close is not None:
            broker_to_close.close()

    def _engine_snapshot(self) -> _EngineSnapshot:
        with self.engine._lock:
            return (
                self.engine.state,
                self.engine.last_trigger_price,
                self.engine.last_trigger_at,
            )

    def _restore_engine_snapshot(self, snapshot: _EngineSnapshot) -> None:
        state, last_trigger_price, last_trigger_at = snapshot
        with self.engine._lock:
            self.engine.state = state
            self.engine.last_trigger_price = last_trigger_price
            self.engine.last_trigger_at = last_trigger_at

    def _handle_trigger(
        self,
        result: TriggerResult,
        quote: Quote,
        engine_snapshot: _EngineSnapshot | None = None,
        trigger_context: _TriggerContext | None = None,
    ) -> bool:
        risk_result = self.risk.check()
        if not risk_result.approved:
            logger.warning(f"risk rejected: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            return False

        try:
            context = trigger_context or _TriggerContext(
                broker=self.broker,
                symbol=self.engine.params.symbol,
                cash_currency=self._cash_currency(),
            )
            executed = False
            if result.action == "BUY":
                executed = self._execute_buy(context.symbol, quote, engine_snapshot, context.broker, context.cash_currency)
            elif result.action == "SELL":
                executed = self._execute_sell(context.symbol, quote, engine_snapshot, context.broker)
            elif result.action == "SELL_SHORT":
                executed = self._execute_sell_short(context.symbol, quote, engine_snapshot, context.broker, context.cash_currency)
            elif result.action == "BUY_TO_COVER":
                executed = self._execute_buy_to_cover(context.symbol, quote, engine_snapshot, context.broker)

            return executed
        except Exception as exc:
            logger.exception(f"order execution failed: {exc}")
            self._record_risk_event(str(exc))
            self.notifier.notify_risk_event("ORDER_FAILED", str(exc))
            return False

    def _execute_buy(
        self,
        symbol: str,
        quote: Quote,
        engine_snapshot: _EngineSnapshot | None = None,
        broker: BrokerGateway | None = None,
        cash_currency: str | None = None,
    ) -> bool:
        broker = broker or self.broker
        cash = broker.get_cash(cash_currency or self._cash_currency())
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return False
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "BUY", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)
        if self._order_status_is_live(order_status):
            self._track_pending_order("BUY", result, engine_snapshot, broker)
            logger.info("BUY pending: %s status=%s", result.broker_order_id, order_status.status)
            return True
        if self._handle_terminal_fill_result("BUY", result, order_status, broker, engine_snapshot):
            return True
        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status)
            logger.warning("BUY not filled: %s status=%s", result.broker_order_id, order_status.status)
            return False
        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._safe_notify_order("BUY", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        logger.info(f"BUY: {symbol} qty={fill_qty} price={fill_price}")
        return True

    def _execute_sell(
        self,
        symbol: str,
        quote: Quote,
        engine_snapshot: _EngineSnapshot | None = None,
        broker: BrokerGateway | None = None,
    ) -> bool:
        broker = broker or self.broker
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return False
        result = broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)
        if self._order_status_is_live(order_status):
            self._track_pending_order("SELL", result, engine_snapshot, broker, avg_price=long_pos.avg_price)
            logger.info("SELL pending: %s status=%s", result.broker_order_id, order_status.status)
            return True
        if self._handle_terminal_fill_result("SELL", result, order_status, broker, engine_snapshot, avg_price=long_pos.avg_price):
            return True
        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status)
            logger.warning("SELL not filled: %s status=%s", result.broker_order_id, order_status.status)
            return False
        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else long_pos.quantity
        pnl = float((fill_price - long_pos.avg_price) * fill_qty)
        self._safe_notify_order("SELL", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        self.risk.record_trade(pnl)
        logger.info(f"SELL: {symbol} qty={fill_qty} price={fill_price} pnl={pnl}")
        return True

    def _execute_sell_short(
        self,
        symbol: str,
        quote: Quote,
        engine_snapshot: _EngineSnapshot | None = None,
        broker: BrokerGateway | None = None,
        cash_currency: str | None = None,
    ) -> bool:
        broker = broker or self.broker
        cash = broker.get_cash(cash_currency or self._cash_currency())
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return False
        usable_cash = (cash * Decimal("0.98")).quantize(Decimal("0.01"))
        qty = int(usable_cash / price)
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "SELL", Decimal(qty), price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "SELL_SHORT", float(qty), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)
        if self._order_status_is_live(order_status):
            self._track_pending_order("SELL_SHORT", result, engine_snapshot, broker)
            logger.info("SELL_SHORT pending: %s status=%s", result.broker_order_id, order_status.status)
            return True
        if self._handle_terminal_fill_result("SELL_SHORT", result, order_status, broker, engine_snapshot):
            return True
        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status)
            logger.warning("SELL_SHORT not filled: %s status=%s", result.broker_order_id, order_status.status)
            return False
        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else Decimal(qty)
        self._safe_notify_order("SELL_SHORT", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        logger.info(f"SELL_SHORT: {symbol} qty={fill_qty} price={fill_price}")
        return True

    def _execute_buy_to_cover(
        self,
        symbol: str,
        quote: Quote,
        engine_snapshot: _EngineSnapshot | None = None,
        broker: BrokerGateway | None = None,
    ) -> bool:
        broker = broker or self.broker
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return False
        result = broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        status = getattr(result, "status", "SUBMITTED")
        self._safe_record_order(result.broker_order_id, symbol, "BUY_TO_COVER", float(pos.quantity), float(price), status)
        order_status = self._wait_for_order_completion(result, broker)
        self._safe_update_order_status_from_result(order_status)
        if self._order_status_is_live(order_status):
            self._track_pending_order("BUY_TO_COVER", result, engine_snapshot, broker, avg_price=pos.avg_price)
            logger.info("BUY_TO_COVER pending: %s status=%s", result.broker_order_id, order_status.status)
            return True
        if self._handle_terminal_fill_result("BUY_TO_COVER", result, order_status, broker, engine_snapshot, avg_price=pos.avg_price):
            return True
        if order_status.status != "FILLED":
            self._pause_after_failed_order(result.broker_order_id, order_status.status)
            logger.warning("BUY_TO_COVER not filled: %s status=%s", result.broker_order_id, order_status.status)
            return False
        fill_price = order_status.executed_price if order_status.executed_price > 0 else price
        fill_qty = order_status.executed_quantity if order_status.executed_quantity > 0 else pos.quantity
        pnl = float((pos.avg_price - fill_price) * fill_qty)
        self._safe_notify_order("BUY_TO_COVER", symbol, str(fill_qty), str(fill_price), result.broker_order_id)
        self.risk.record_trade(pnl)
        logger.info(f"BUY_TO_COVER: {symbol} qty={fill_qty} price={fill_price} pnl={pnl}")
        return True

    @staticmethod
    def _order_status_is_live(result: object) -> bool:
        return getattr(result, "status", "SUBMITTED") in _LIVE_ORDER_STATUSES

    def _track_pending_order(
        self,
        action: str,
        result: OrderResult,
        engine_snapshot: _EngineSnapshot | None,
        broker: BrokerGateway,
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
            engine_snapshot=engine_snapshot if engine_snapshot is not None else self._engine_snapshot(),
            avg_price=avg_price,
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
        )
        with self._state_lock:
            self._pending_order = pending

    def _clear_pending_order(self, order_id: str) -> None:
        with self._state_lock:
            if self._pending_order is not None and self._pending_order.broker_order_id == order_id:
                self._pending_order = None

    def _reconcile_pending_order(self, pending: _PendingOrder) -> None:
        now = time.monotonic()
        if now < pending.next_status_check_at:
            return
        pending.next_status_check_at = now + self._order_status_poll_interval_seconds
        try:
            order_status = pending.broker.get_order_status(pending.broker_order_id)
        except Exception:
            logger.exception("failed to query pending order status for %s", pending.broker_order_id)
            return

        self._safe_update_order_status_from_result(order_status)
        status = getattr(order_status, "status", "SUBMITTED")
        if status == "FILLED":
            self._finalize_pending_fill(pending, order_status)
            self._clear_pending_order(pending.broker_order_id)
            self._broadcast_status()
            return
        if status in _FAILED_ORDER_STATUSES:
            fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
            if fill_qty > 0:
                self._finalize_pending_fill(pending, order_status, fill_qty)
                self._clear_pending_order(pending.broker_order_id)
                if self._should_restore_after_partial_terminal_fill(pending, fill_qty):
                    with self._state_lock:
                        self._restore_engine_snapshot(pending.engine_snapshot)
                self._broadcast_status()
                return
            self._pause_after_failed_order(pending.broker_order_id, status)
            self._clear_pending_order(pending.broker_order_id)
            with self._state_lock:
                self._restore_engine_snapshot(pending.engine_snapshot)
            self._broadcast_status()
            return
        logger.debug("pending order still live: %s status=%s", pending.broker_order_id, status)

    def _finalize_pending_fill(self, pending: _PendingOrder, order_status: object, fill_qty: Decimal | None = None) -> None:
        fill_price = self._resolved_decimal(order_status, "executed_price", pending.price)
        fill_qty = fill_qty if fill_qty is not None else self._resolved_decimal(order_status, "executed_quantity", pending.quantity)
        if pending.action == "SELL":
            avg_price = pending.avg_price if pending.avg_price is not None else pending.price
            pnl = float((fill_price - avg_price) * fill_qty)
            self._safe_notify_order("SELL", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            self.risk.record_trade(pnl)
            logger.info(f"SELL filled: {pending.symbol} qty={fill_qty} price={fill_price} pnl={pnl}")
            return
        if pending.action == "BUY_TO_COVER":
            avg_price = pending.avg_price if pending.avg_price is not None else pending.price
            pnl = float((avg_price - fill_price) * fill_qty)
            self._safe_notify_order("BUY_TO_COVER", pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
            self.risk.record_trade(pnl)
            logger.info(f"BUY_TO_COVER filled: {pending.symbol} qty={fill_qty} price={fill_price} pnl={pnl}")
            return
        self._safe_notify_order(pending.action, pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
        logger.info(f"{pending.action} filled: {pending.symbol} qty={fill_qty} price={fill_price}")

    @staticmethod
    def _should_restore_after_partial_terminal_fill(pending: _PendingOrder, fill_qty: Decimal) -> bool:
        return pending.action in {"SELL", "BUY_TO_COVER"} and fill_qty < pending.quantity

    def _handle_terminal_fill_result(
        self,
        action: str,
        result: OrderResult,
        order_status: object,
        broker: BrokerGateway,
        engine_snapshot: _EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
    ) -> bool:
        status = getattr(order_status, "status", "SUBMITTED")
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
            engine_snapshot=engine_snapshot if engine_snapshot is not None else self._engine_snapshot(),
            avg_price=avg_price,
        )
        self._finalize_pending_fill(pending, order_status, fill_qty)
        if engine_snapshot is not None and self._should_restore_after_partial_terminal_fill(pending, fill_qty):
            with self._state_lock:
                self._restore_engine_snapshot(engine_snapshot)
        return True

    def _pause_after_failed_order(self, order_id: str, status: str) -> None:
        reason = f"order {order_id} ended with status {status}"
        self.risk.pause(reason)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record order failure risk event for %s", order_id)
        try:
            self.notifier.notify_risk_event("ORDER_FAILED", reason)
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

    def _record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        db = SessionLocal()
        try:
            order = OrderRecord(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                status=status,
            )
            db.add(order)
            db.commit()
        finally:
            db.close()

    def _safe_record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        try:
            self._record_order(order_id, symbol, side, qty, price, status)
        except Exception:
            logger.exception("failed to record order %s for %s (broker order is still live)", order_id, symbol)

    def _update_order_status(self, order_id: str, status: str, filled_at: datetime | None = None) -> None:
        db = SessionLocal()
        try:
            order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == order_id).order_by(OrderRecord.id.desc()).first()
            if order is None:
                logger.warning("cannot update missing order %s to status %s", order_id, status)
                return
            order.status = status
            if filled_at is not None:
                order.filled_at = filled_at
            db.add(order)
            db.commit()
        finally:
            db.close()

    def _safe_update_order_status(self, order_id: str, status: str, filled_at: datetime | None = None) -> None:
        try:
            self._update_order_status(order_id, status, filled_at)
        except Exception:
            logger.exception("failed to update order %s to status %s", order_id, status)

    def _safe_update_order_status_from_result(self, result: object) -> None:
        status = getattr(result, "status", "SUBMITTED")
        if status == "SUBMITTED":
            return
        filled_at = datetime.now(timezone.utc) if status == "FILLED" else None
        self._safe_update_order_status(getattr(result, "broker_order_id", ""), status, filled_at)

    def _cash_currency(self) -> str:
        return "HKD" if self.engine.params.market == "HK" else "USD"

    def _wait_for_order_completion(self, result: OrderResult, broker: BrokerGateway | None = None) -> object:
        broker = broker or self.broker
        status = getattr(result, "status", "SUBMITTED")
        last_status = SimpleNamespace(
            broker_order_id=result.broker_order_id,
            status=status,
            executed_quantity=getattr(result, "quantity", Decimal("0")) if status == "FILLED" else Decimal("0"),
            executed_price=getattr(result, "price", Decimal("0")) if status == "FILLED" else Decimal("0"),
        )
        if status in {"FILLED", "REJECTED", "CANCELLED"}:
            return last_status

        deadline = time.monotonic() + self._order_status_timeout_seconds
        while True:
            try:
                last_status = broker.get_order_status(result.broker_order_id)
            except Exception:
                logger.exception("failed to query order status for %s", result.broker_order_id)
                return last_status
            if getattr(last_status, "status", "SUBMITTED") in {"FILLED", "REJECTED", "CANCELLED"}:
                return last_status
            if time.monotonic() >= deadline:
                return last_status
            time.sleep(self._order_status_poll_interval_seconds)

    def _safe_notify_order(self, side: str, symbol: str, quantity: str, price: str, order_id: str) -> None:
        try:
            self.notifier.notify_order(side, symbol, quantity, price, order_id)
        except Exception:
            logger.exception("failed to send order notification for %s %s", side, symbol)

    # Long-running reconciliation or broker webhooks can extend this short
    # confirmation loop if orders regularly remain open past the timeout.

    def _record_risk_event(self, reason: str) -> None:
        db = SessionLocal()
        try:
            event = RiskEvent(event_type="RISK_REJECTION", reason=reason)
            db.add(event)
            db.commit()
        finally:
            db.close()

    def _broadcast_status(self) -> None:
        try:
            data = self.engine.to_dict()
            data["risks"] = {
                "daily_pnl": self.risk.daily_pnl,
                "consecutive_losses": self.risk.consecutive_losses,
                "kill_switch": self.risk.kill_switch,
                "paused": self.risk.paused,
            }
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.broadcast(data), self._loop)
        except Exception:
            logger.warning("broadcast failed")

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._persist_state()
            except Exception:
                logger.exception("error persisting state")
            time.sleep(5)

    def _persist_state(self) -> None:
        with self._state_lock:
            engine_state = self.engine.state.value
            last_price = self.engine.last_price
            last_trigger_price = self.engine.last_trigger_price
            last_trigger_at = self.engine.last_trigger_at
            daily_pnl = self.risk.daily_pnl
            consecutive_losses = self.risk.consecutive_losses
            kill_switch = self.risk.kill_switch
            paused = self.risk.paused

        db = SessionLocal()
        try:
            svc = StrategyService(db)
            svc.update_runtime_state(
                engine_state=engine_state,
                last_price=last_price,
                daily_pnl=daily_pnl,
                consecutive_losses=consecutive_losses,
                kill_switch=kill_switch,
                paused=paused,
                last_trigger_price=last_trigger_price,
                last_trigger_at=last_trigger_at,
            )
        finally:
            db.close()


_runner: AppRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AppRunner()
    return _runner
