from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from datetime import datetime
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerCredentials, BrokerGateway, Quote
from app.core.engine import StrategyEngine, StrategyParams, TriggerResult, EngineState
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, RiskEvent
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.strategy_service import StrategyService
from app.api.ws import manager

logger = logging.getLogger("auto_trade.runner")


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
            self.engine.state = EngineState(state.engine_state)
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

    def start(self) -> None:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
        with self._start_lock:
            if self._running:
                return
            self._running = True
            self._initialize_runner()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("runner started")

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
            new_notifier = ServerChanNotifier(credentials.sct_key or settings.sct_key)

            new_broker = BrokerGateway(
                BrokerCredentials(
                    app_key=credentials.longbridge_app_key,
                    app_secret=credentials.longbridge_app_secret,
                    access_token=credentials.longbridge_access_token,
                )
            )

            if should_resubscribe:
                try:
                    new_broker.subscribe_quotes(symbol, self._on_quote)
                except Exception as exc:
                    logger.warning(f"cannot subscribe quotes after credential reload: {exc}")
                    self.notifier = new_notifier
                    new_broker.close()
                    return

            old_broker = self.broker
            old_broker.close()
            self.broker = new_broker
            self.notifier = new_notifier

            if should_resubscribe:
                self._quotes_subscribed = True
            elif not resubscribe:
                self._quotes_subscribed = False

    def reload_credentials(self) -> None:
        self._apply_credentials(self._load_credentials(), resubscribe=self._running)

    def stop(self) -> None:
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
            self.broker.close()

    def _on_quote(self, quote: Quote) -> None:
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
                executed = self._handle_trigger(result, quote)
                if not executed:
                    self._restore_engine_snapshot(engine_snapshot)
                    self._broadcast_status()
        except Exception:
            logger.exception("error processing quote")

    def _restore_engine_snapshot(self, snapshot: tuple[EngineState, float, datetime | None]) -> None:
        state, last_trigger_price, last_trigger_at = snapshot
        self.engine.state = state
        self.engine.last_trigger_price = last_trigger_price
        self.engine.last_trigger_at = last_trigger_at

    def _handle_trigger(self, result: TriggerResult, quote: Quote) -> bool:
        risk_result = self.risk.check()
        if not risk_result.approved:
            logger.warning(f"risk rejected: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            return False

        try:
            symbol = self.engine.params.symbol
            executed = False
            if result.action == "BUY":
                executed = self._execute_buy(symbol, quote)
            elif result.action == "SELL":
                executed = self._execute_sell(symbol, quote)
            elif result.action == "SELL_SHORT":
                executed = self._execute_sell_short(symbol, quote)
            elif result.action == "BUY_TO_COVER":
                executed = self._execute_buy_to_cover(symbol, quote)

            if executed:
                self._broadcast_status()
            return executed
        except Exception as exc:
            logger.exception(f"order execution failed: {exc}")
            self._record_risk_event(str(exc))
            self.notifier.notify_risk_event("ORDER_FAILED", str(exc))
            return False

    def _execute_buy(self, symbol: str, quote: Quote) -> bool:
        broker = self.broker
        cash = broker.get_cash()
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return False
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            logger.warning("BUY: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "BUY", qty, price)
        self._record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price))
        self.notifier.notify_order("BUY", symbol, str(qty), str(price), result.broker_order_id)
        logger.info(f"BUY: {symbol} qty={qty} price={price}")
        return True

    def _execute_sell(self, symbol: str, quote: Quote) -> bool:
        broker = self.broker
        positions = broker.get_positions()
        long_pos = next((p for p in positions if p.symbol == symbol and p.side == "LONG"), None)
        if long_pos is None:
            logger.warning("SELL: no long position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL: price <= 0, price=%s", price)
            return False
        pnl = (float(price) - float(long_pos.avg_price)) * float(long_pos.quantity)
        result = broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price))
        self.notifier.notify_order("SELL", symbol, str(long_pos.quantity), str(price), result.broker_order_id)
        self.risk.record_trade(pnl)
        logger.info(f"SELL: {symbol} qty={long_pos.quantity} price={price} pnl={pnl}")
        return True

    def _execute_sell_short(self, symbol: str, quote: Quote) -> bool:
        broker = self.broker
        cash = broker.get_cash()
        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return False
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            logger.warning("SELL_SHORT: qty <= 0, cash=%s price=%s", cash, price)
            return False

        result = broker.submit_limit_order(symbol, "SELL", qty, price)
        self._record_order(result.broker_order_id, symbol, "SELL_SHORT", float(qty), float(price))
        self.notifier.notify_order("SELL_SHORT", symbol, str(qty), str(price), result.broker_order_id)
        self.risk.record_trade(0.0)
        logger.info(f"SELL_SHORT: {symbol} qty={qty} price={price}")
        return True

    def _execute_buy_to_cover(self, symbol: str, quote: Quote) -> bool:
        broker = self.broker
        positions = broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.side == "SHORT" and p.quantity > 0), None)
        if pos is None:
            logger.warning("BUY_TO_COVER: no short position for %s", symbol)
            return False

        price = Decimal(str(quote.last_price))
        if price <= 0:
            logger.warning("BUY_TO_COVER: price <= 0, price=%s", price)
            return False
        pnl = (float(pos.avg_price) - float(price)) * float(pos.quantity)
        result = broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "BUY_TO_COVER", float(pos.quantity), float(price))
        self.notifier.notify_order("BUY_TO_COVER", symbol, str(pos.quantity), str(price), result.broker_order_id)
        self.risk.record_trade(pnl)
        logger.info(f"BUY_TO_COVER: {symbol} qty={pos.quantity} price={price} pnl={pnl}")
        return True

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
        db = SessionLocal()
        try:
            svc = StrategyService(db)
            svc.update_runtime_state(
                engine_state=self.engine.state,
                last_price=self.engine.last_price,
                daily_pnl=self.risk.daily_pnl,
                consecutive_losses=self.risk.consecutive_losses,
                kill_switch=self.risk.kill_switch,
                paused=self.risk.paused,
                last_trigger_price=self.engine.last_trigger_price,
                last_trigger_at=self.engine.last_trigger_at,
            )
        finally:
            db.close()


_runner: AppRunner | None = None


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        _runner = AppRunner()
    return _runner
