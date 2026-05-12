from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from decimal import Decimal

from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import StrategyEngine, StrategyParams, TriggerResult
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, RiskEvent
from app.services.strategy_service import StrategyService
from app.api.ws import manager

logger = logging.getLogger("auto_trade.runner")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController()
        self.notifier = ServerChanNotifier("")
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._running:
            return
        self._running = True

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
            self.engine.state = state.engine_state
            self.engine.last_price = state.last_price

            self.risk.config = RiskConfig(
                max_daily_loss=config.max_daily_loss,
                max_consecutive_losses=config.max_consecutive_losses,
            )
            self.risk.daily_pnl = state.daily_pnl
            self.risk.consecutive_losses = state.consecutive_losses
            self.risk.kill_switch = state.kill_switch
            self.risk.paused = state.paused

            self.notifier = ServerChanNotifier(config.sct_key)

            if config.symbol:
                try:
                    self.broker.subscribe_quotes(config.symbol, self._on_quote)
                    logger.info(f"subscribed to {config.symbol} quotes")
                except Exception as exc:
                    logger.warning(f"cannot subscribe quotes (SDK may not be available): {exc}")
        finally:
            db.close()

        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        logger.info("runner started")

    def stop(self) -> None:
        self._running = False

    def _on_quote(self, quote: Quote) -> None:
        try:
            result = self.engine.update_price(quote.last_price)
            self._broadcast_status()

            if result.triggered:
                self._handle_trigger(result, quote)
        except Exception:
            logger.exception("error processing quote")

    def _handle_trigger(self, result: TriggerResult, quote: Quote) -> None:
        risk_result = self.risk.check()
        if not risk_result.approved:
            logger.warning(f"risk rejected: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            return

        try:
            symbol = self.engine.params.symbol
            if result.action == "BUY":
                self._execute_buy(symbol, quote)
            elif result.action == "SELL":
                self._execute_sell(symbol, quote)
            elif result.action == "SELL_SHORT":
                self._execute_sell_short(symbol, quote)
            elif result.action == "BUY_TO_COVER":
                self._execute_buy_to_cover(symbol, quote)

            self._broadcast_status()
        except Exception as exc:
            logger.exception(f"order execution failed: {exc}")
            self._record_risk_event(str(exc))
            self.notifier.notify_risk_event("ORDER_FAILED", str(exc))

    def _execute_buy(self, symbol: str, quote: Quote) -> None:
        cash = self.broker.get_cash()
        price = Decimal(str(quote.last_price))
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            return

        result = self.broker.submit_limit_order(symbol, "BUY", qty, price)
        self._record_order(result.broker_order_id, symbol, "BUY", float(qty), float(price))
        self.notifier.notify_order("BUY", symbol, str(qty), str(price), result.broker_order_id)
        logger.info(f"BUY: {symbol} qty={qty} price={price}")

    def _execute_sell(self, symbol: str, quote: Quote) -> None:
        positions = self.broker.get_positions()
        long_pos = next((p for p in positions if p.side == "LONG"), None)
        if long_pos is None:
            return

        price = Decimal(str(quote.last_price))
        result = self.broker.submit_limit_order(symbol, "SELL", long_pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "SELL", float(long_pos.quantity), float(price))
        self.notifier.notify_order("SELL", symbol, str(long_pos.quantity), str(price), result.broker_order_id)
        logger.info(f"SELL: {symbol} qty={long_pos.quantity} price={price}")

    def _execute_sell_short(self, symbol: str, quote: Quote) -> None:
        cash = self.broker.get_cash()
        price = Decimal(str(quote.last_price))
        qty = (cash / price).quantize(Decimal("0.01"))
        if qty <= 0:
            return

        result = self.broker.submit_limit_order(symbol, "SELL", qty, price)
        self._record_order(result.broker_order_id, symbol, "SELL", float(qty), float(price))
        self.notifier.notify_order("SELL_SHORT", symbol, str(qty), str(price), result.broker_order_id)
        logger.info(f"SELL_SHORT: {symbol} qty={qty} price={price}")

    def _execute_buy_to_cover(self, symbol: str, quote: Quote) -> None:
        positions = self.broker.get_positions()
        pos = next((p for p in positions if p.symbol == symbol and p.quantity > 0), None)
        if pos is None:
            return

        price = Decimal(str(quote.last_price))
        result = self.broker.submit_limit_order(symbol, "BUY", pos.quantity, price)
        self._record_order(result.broker_order_id, symbol, "BUY", float(pos.quantity), float(price))
        self.notifier.notify_order("BUY_TO_COVER", symbol, str(pos.quantity), str(price), result.broker_order_id)
        logger.info(f"BUY_TO_COVER: {symbol} qty={pos.quantity} price={price}")

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
            asyncio.run(manager.broadcast(data))
        except Exception:
            pass

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
            )
        finally:
            db.close()


_runner: AppRunner | None = None


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        _runner = AppRunner()
    return _runner
