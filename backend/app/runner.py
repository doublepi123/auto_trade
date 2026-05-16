from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from datetime import datetime

from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import StrategyEngine, StrategyParams, TriggerResult, EngineState
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, RiskEvent
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService
from app.services.trade_execution_service import TradeExecutionService
from app.api.ws import manager

logger = logging.getLogger("auto_trade.runner")


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self.runtime_state = RuntimeStateService()
        self.trade_execution = TradeExecutionService(
            record_order=self._safe_record_order,
            record_risk_event=self._record_risk_event,
        )
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._quotes_subscribed = False

    def _initialize_runner(self) -> None:
        db = SessionLocal()
        try:
            svc = StrategyService(db)
            config = svc.get_config()
            self.runtime_state.load(svc, self.engine, self.risk)

            self._apply_credentials(self._load_credentials(), resubscribe=False)

            try:
                self._loop = asyncio.get_running_loop()
            except RuntimeError:
                self._loop = None

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
            symbol = self.engine.params.symbol
            should_resubscribe = resubscribe and bool(symbol)
            new_notifier = ServerChanNotifier(credentials.sct_key if credentials.sct_key else settings.sct_key)

            if credentials.longbridge_app_key:
                os.environ["LONGPORT_APP_KEY"] = credentials.longbridge_app_key
            if credentials.longbridge_app_secret:
                os.environ["LONGPORT_APP_SECRET"] = credentials.longbridge_app_secret
            if credentials.longbridge_access_token:
                os.environ["LONGPORT_ACCESS_TOKEN"] = credentials.longbridge_access_token

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
                            logger.info(f"re-subscribed to {config.symbol} after strategy reload")
                        except Exception as exc:
                            logger.error(f"quote subscription failed after strategy reload: {exc}")
            finally:
                db.close()

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
            self.broker.close()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=10)

    def _on_quote(self, quote: Quote) -> None:
        with self._state_lock:
            if not self._running:
                return
            try:
                engine_snapshot = (
                    self.engine.state,
                    self.engine.last_trigger_price,
                    self.engine.last_trigger_at,
                )
                result = self.engine.update_price(quote.last_price)
                self._broadcast_status()

                if result.triggered:
                    try:
                        executed = self._handle_trigger(result, quote)
                        if not executed:
                            self._restore_engine_snapshot(engine_snapshot)
                            self._broadcast_status()
                    except Exception:
                        self._restore_engine_snapshot(engine_snapshot)
                        self._broadcast_status()
                        raise
            except Exception:
                logger.exception("error processing quote")

    def _restore_engine_snapshot(self, snapshot: tuple[EngineState, float, datetime | None]) -> None:
        state, last_trigger_price, last_trigger_at = snapshot
        with self.engine._lock:
            self.engine.state = state
            self.engine.last_trigger_price = last_trigger_price
            self.engine.last_trigger_at = last_trigger_at

    def _handle_trigger(self, result: TriggerResult, quote: Quote) -> bool:
        executed = self.trade_execution.execute(
            result.action,
            self.engine.params.symbol,
            quote,
            self.broker,
            self.risk,
            self.notifier,
        )
        if executed:
            self._broadcast_status()
        return executed

    def _record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        db = SessionLocal()
        try:
            order = OrderRecord(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=qty,
                price=price,
                status="SUBMITTED",
            )
            db.add(order)
            db.commit()
        finally:
            db.close()

    def _safe_record_order(self, order_id: str, symbol: str, side: str, qty: float, price: float) -> None:
        try:
            self._record_order(order_id, symbol, side, qty, price)
        except Exception:
            logger.exception("failed to record order %s for %s (broker order is still live)", order_id, symbol)

    # TODO: Implement order status polling or broker webhook integration to update
    # order status from SUBMITTED to FILLED/REJECTED/CANCELLED using broker SDK.

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
            snapshot = self.runtime_state.snapshot(self.engine, self.risk)

        db = SessionLocal()
        try:
            svc = StrategyService(db)
            self.runtime_state.persist_snapshot(svc, snapshot)
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
