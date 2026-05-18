from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Generator

from app.api.ws import manager
from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService
from app.services.trade_execution_service import TradeExecutionService

logger = logging.getLogger("auto_trade.runner")

_EngineSnapshot = tuple[EngineState, float, datetime | None]


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self._trade_svc = TradeExecutionService(
            record_order=self._record_order,
            update_order_status=self._update_order_status,
            record_risk_event=self._record_risk_event,
        )
        self._state_svc = RuntimeStateService()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._quotes_subscribed = False
        self._trigger_in_flight = False
        self._defer_broker_close = False

    def _initialize_runner(self) -> None:
        with self._db_session() as db:
            self._state_svc.load(db, self.engine, self.risk)
            self._pause_if_unresolved_live_order_exists(db)
            self._apply_credentials(self._load_credentials(), resubscribe=False)

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        symbol = self.engine.params.symbol
        if symbol and not self._quotes_subscribed:
            try:
                self.broker.subscribe_quotes(symbol, self._on_quote)
                self._quotes_subscribed = True
                logger.info("subscribed to %s quotes", symbol)
            except Exception as exc:
                logger.error("quote subscription failed for %s: %s", symbol, exc)
                logger.error("system running without quote updates")

    def _pause_if_unresolved_live_order_exists(self, db) -> None:
        order = (
            db.query(OrderRecord)
            .filter(OrderRecord.status.in_({"SUBMITTED", "PARTIAL_FILLED"}))
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

    @property
    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        defer_broker_close = False
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
            if self._trigger_in_flight:
                self._defer_broker_close = True
                defer_broker_close = True
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)
        if defer_broker_close:
            return
        self.broker.close()

    def reload_credentials(self) -> None:
        self._apply_credentials(self._load_credentials(), resubscribe=self._running)

    def reload_strategy(self) -> None:
        with self._state_lock:
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
                            logger.info("re-subscribed to %s after strategy reload", config.symbol)
                        except Exception as exc:
                            logger.error("quote subscription failed after strategy reload: %s", exc)
            finally:
                db.close()

    def _on_quote(self, quote: Quote) -> None:
        processing_started = False
        result = None
        engine_snapshot = None
        trigger_symbol = None
        try:
            with self._state_lock:
                if not self._running or self._trigger_in_flight:
                    return
                if self._trade_svc.has_pending_order:
                    self._trigger_in_flight = True
                    processing_started = True
                else:
                    engine_snapshot = self._engine_snapshot()
                    result = self.engine.update_price(quote.last_price)
                    if result.triggered:
                        trigger_symbol = self.engine.params.symbol
                        self._trigger_in_flight = True
                        processing_started = True

            if self._trade_svc.has_pending_order and processing_started:
                self._trade_svc.reconcile(
                    self.risk,
                    self.notifier,
                    self._restore_engine_snapshot,
                    self.notifier.notify_risk_event,
                )
                self._broadcast_status()
                return

            self._broadcast_status()

            if result is None or not result.triggered:
                return

            risk_result = self.risk.check()
            if not risk_result.approved:
                logger.warning("risk rejected: %s", risk_result.reason)
                self._record_risk_event(risk_result.reason)
                self.notifier.notify_risk_event("REJECTED", risk_result.reason)
                if engine_snapshot is not None:
                    self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
                return

            try:
                order_status = self._trade_svc.execute(
                    action=result.action,
                    symbol=trigger_symbol or self.engine.params.symbol,
                    quote=quote,
                    broker=self.broker,
                    risk=self.risk,
                    notifier=self.notifier,
                    cash_currency=self._cash_currency(),
                    engine_snapshot=engine_snapshot,
                    restore_engine_snapshot=self._restore_engine_snapshot,
                    notify_risk_event=self.notifier.notify_risk_event,
                )
                if order_status is None or order_status.status in {"REJECTED", "CANCELLED"}:
                    if engine_snapshot is not None:
                        self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
            except Exception:
                if engine_snapshot is not None:
                    self._restore_engine_snapshot(engine_snapshot)
                self._broadcast_status()
                raise
        except Exception:
            logger.exception("error processing quote")
        finally:
            if processing_started:
                close_broker = False
                with self._state_lock:
                    self._trigger_in_flight = False
                    if not self._running and self._defer_broker_close:
                        self._defer_broker_close = False
                        close_broker = True
                if close_broker:
                    self.broker.close()

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

    def _cash_currency(self) -> str:
        return "HKD" if self.engine.params.market == "HK" else "USD"

    def _broadcast_status(self) -> None:
        try:
            data = self.engine.to_dict()
            data["risks"] = {
                "daily_pnl": self.risk.daily_pnl,
                "consecutive_losses": self.risk.consecutive_losses,
                "kill_switch": self.risk.kill_switch,
                "paused": self.risk.paused,
            }
            data["runner_running"] = self.is_running
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.broadcast(data), self._loop)
        except Exception:
            logger.warning("broadcast failed")

    def _run_loop(self) -> None:
        while self._running:
            try:
                if self._trade_svc.has_pending_order:
                    self._trade_svc.reconcile(
                        self.risk,
                        self.notifier,
                        self._restore_engine_snapshot,
                        self.notifier.notify_risk_event,
                    )
                    self._broadcast_status()
            except Exception:
                logger.exception("error reconciling pending orders")
            try:
                with self._db_session() as db:
                    self._state_svc.persist(db, self.engine, self.risk)
            except Exception:
                logger.exception("error persisting state")
            time.sleep(5)

    @staticmethod
    @contextmanager
    def _db_session() -> Generator:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _load_credentials(self) -> PlainCredentials:
        with self._db_session() as db:
            return CredentialsService(db).get_plain_credentials()

    def _apply_credentials(self, credentials: PlainCredentials, *, resubscribe: bool) -> None:
        with self._state_lock:
            symbol = self.engine.params.symbol
            should_resubscribe = resubscribe and bool(symbol)
            new_notifier = ServerChanNotifier(
                credentials.sct_key if credentials.sct_key else settings.sct_key
            )

            self._set_or_clear_env(
                "LONGPORT_APP_KEY",
                credentials.longbridge_app_key or settings.longbridge_app_key,
            )
            self._set_or_clear_env(
                "LONGPORT_APP_SECRET",
                credentials.longbridge_app_secret or settings.longbridge_app_secret,
            )
            self._set_or_clear_env(
                "LONGPORT_ACCESS_TOKEN",
                credentials.longbridge_access_token or settings.longbridge_access_token,
            )

            new_broker = BrokerGateway()

            if should_resubscribe:
                try:
                    new_broker.subscribe_quotes(symbol, self._on_quote)
                except Exception as exc:
                    logger.warning("cannot subscribe quotes after credential reload: %s", exc)
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

    @staticmethod
    def _set_or_clear_env(name: str, value: str) -> None:
        if value:
            os.environ[name] = value
        else:
            os.environ.pop(name, None)

    def _record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float, status: str = "SUBMITTED") -> None:
        with self._db_session() as db:
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

    def _update_order_status(
        self,
        order_id: str,
        status: str,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
    ) -> None:
        with self._db_session() as db:
            order = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == order_id)
                .order_by(OrderRecord.id.desc())
                .first()
            )
            if order is None:
                logger.warning("cannot update missing order %s to status %s", order_id, status)
                return
            order.status = status
            if filled_at is not None:
                order.filled_at = filled_at
            if executed_quantity is not None:
                order.executed_quantity = executed_quantity
            if executed_price is not None:
                order.executed_price = executed_price
            db.add(order)
            db.commit()

    def _record_risk_event(self, reason: str) -> None:
        with self._db_session() as db:
            self._state_svc.record_risk_event(db, reason)


_runner: AppRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AppRunner()
    return _runner
