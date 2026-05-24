from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Generator, Optional

from app.api.ws import manager
from app.config import settings
from app.core.broker import BrokerGateway, Quote
from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.market_calendar import is_trading_hours, trade_day_for
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, TrackedEntry
from app.services.daily_pnl_service import DailyPnlService
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService
from app.services.trade_event_service import record_trade_event
from app.services.trade_execution_service import TradeExecutionService

logger = logging.getLogger("auto_trade.runner")

_EngineSnapshot = tuple[EngineState, float, Optional[datetime]]
_LLM_ORDER_ACTION_MAP = {
    "BUY_NOW": "BUY",
    "SELL_NOW": "SELL",
    "SELL_SHORT_NOW": "SELL_SHORT",
    "BUY_TO_COVER_NOW": "BUY_TO_COVER",
    "STOP_LOSS_SELL_NOW": "SELL",
    "STOP_LOSS_COVER_NOW": "BUY_TO_COVER",
}
_LLM_STOP_LOSS_ACTIONS = {"STOP_LOSS_SELL_NOW", "STOP_LOSS_COVER_NOW"}
_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_TERMINAL_ORDER_STATUSES = {"FILLED", "REJECTED", "CANCELLED"}


class AppRunner:
    def __init__(self) -> None:
        self.broker = BrokerGateway()
        self.engine = StrategyEngine()
        self.risk = RiskController(trade_day_provider=self._market_trade_day)
        self.notifier = ServerChanNotifier("")
        self._trade_svc = TradeExecutionService(
            record_order=self._record_order,
            update_order_status=self._update_order_status,
            record_risk_event=self._record_risk_event,
            record_order_skipped=self._record_order_skipped,
            persist_entry=self._persist_tracked_entry,
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
        self._last_quote_at = 0.0
        self._last_push_quote_at = 0.0
        self._last_active_quote_refresh_at = 0.0
        self._active_quote_refresh_interval_seconds = 15.0
        self._quote_resubscribe_threshold_seconds = 90.0
        self._last_position_sync_at = 0.0
        self._position_sync_interval_seconds = 15.0
        self._last_order_sync_at = 0.0
        self._order_sync_interval_seconds = 15.0
        self._recent_quote_window_seconds = 300.0
        self._recent_quotes_cap = 500
        self._recent_quotes: list[dict[str, Any]] = []
        self._last_action_message = ""

    def _initialize_runner(self) -> None:
        with self._db_session() as db:
            self._state_svc.load(db, self.engine, self.risk)
            self._load_tracked_entries(db)
            self._apply_credentials(self._load_credentials(), resubscribe=False)

        self.sync_today_orders_from_broker(force=True)
        self._sync_risk_from_order_ledger()
        with self._db_session() as db:
            self._pause_if_unresolved_live_order_exists(db)
            self._reconcile_tracked_entries_with_broker(db)

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        symbol = self.engine.params.symbol
        self._reset_quote_tracking(clear_history=True)
        if symbol and not self._quotes_subscribed:
            try:
                self.broker.subscribe_quotes(symbol, self._on_quote)
                self._quotes_subscribed = True
                self._last_push_quote_at = time.monotonic()
                logger.info("subscribed to %s quotes", symbol)
            except Exception as exc:
                logger.error("quote subscription failed for %s: %s", symbol, exc)
                logger.error("system running without quote updates")

    def _pause_if_unresolved_live_order_exists(self, db) -> None:
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

    def start(self, *, loop: asyncio.AbstractEventLoop | None = None) -> bool:
        with self._start_lock:
            if self._running:
                return False
            if loop is not None:
                self._loop = loop
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
                    min_profit_amount=config.min_profit_amount,
                    auto_resume_minutes=config.auto_resume_minutes,
                )
                self.risk.config = RiskConfig(
                    max_daily_loss=config.max_daily_loss,
                    max_consecutive_losses=config.max_consecutive_losses,
                )
                if config.symbol != old_symbol and self._running:
                    self._reset_quote_tracking(clear_history=True)
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
                            self._last_push_quote_at = time.monotonic()
                            logger.info("re-subscribed to %s after strategy reload", config.symbol)
                        except Exception as exc:
                            logger.error("quote subscription failed after strategy reload: %s", exc)
            finally:
                db.close()

    def _on_quote(self, quote: Quote, *, is_push: bool = True) -> None:
        processing_started = False
        result = None
        engine_snapshot = None
        trigger_symbol = None
        try:
            with self._state_lock:
                if is_push:
                    self._last_push_quote_at = time.monotonic()
                self._remember_quote(quote)
                if not self._running or self._trigger_in_flight:
                    return
                if self._trade_svc.has_pending_order:
                    self._trigger_in_flight = True
                    processing_started = True
                elif not self.risk.check().approved:
                    self.engine.record_price(quote.last_price)
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
            self._set_last_action_message(result.description)

            risk_result = self.risk.check()
            if not risk_result.approved:
                logger.warning("risk rejected: %s", risk_result.reason)
                self._set_last_action_message(f"{result.action} rejected by risk: {risk_result.reason}")
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
                    min_profit_amount=self.engine.params.min_profit_amount,
                    engine_snapshot=engine_snapshot,
                    restore_engine_snapshot=self._restore_engine_snapshot,
                    notify_risk_event=self.notifier.notify_risk_event,
                )
                if order_status is None:
                    self._set_last_action_message(f"{result.action} skipped: no order submitted")
                    if engine_snapshot is not None:
                        self._restore_engine_snapshot(engine_snapshot)
                elif order_status.status == "SKIPPED":
                    reason = order_status.reason or "order skipped"
                    self._set_last_action_message(f"{result.action} skipped: {reason}")
                    if engine_snapshot is not None:
                        self._restore_engine_state_preserve_trigger(engine_snapshot)
                elif order_status.status in {"REJECTED", "CANCELLED"}:
                    self._set_last_action_message(f"{result.action} ended with status {order_status.status}")
                    if engine_snapshot is not None:
                        self._restore_engine_snapshot(engine_snapshot)
                else:
                    order_id = order_status.broker_order_id or "-"
                    self._set_last_action_message(f"{result.action} {order_status.status}: {order_id}")
                self._broadcast_status()
            except Exception as exc:
                if engine_snapshot is not None:
                    self._restore_engine_snapshot(engine_snapshot)
                reason = f"order execution failed: {exc}"
                self._set_last_action_message(reason)
                self.risk.pause(
                    reason,
                    auto_resumable=self._is_auto_resumable_pause_reason(reason),
                )
                try:
                    self._record_risk_event(reason)
                except Exception:
                    logger.exception("failed to record order execution exception risk event")
                try:
                    self.notifier.notify_risk_event("ORDER_FAILED", reason)
                except Exception:
                    logger.exception("failed to send order execution exception notification")
                self._broadcast_status()
                logger.exception("order execution failed; trading paused")
                return
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

    def _restore_engine_state_preserve_trigger(self, snapshot: _EngineSnapshot) -> None:
        state, _last_trigger_price, _last_trigger_at = snapshot
        with self.engine._lock:
            self.engine.state = state

    def execute_llm_order_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        action = str(decision.get("order_action") or "NONE").upper()
        if action == "NONE":
            return {"executed": False, "status": "NO_ACTION", "order_id": None, "action": ""}
        if not self._running:
            return {"executed": False, "status": "RUNNER_STOPPED", "order_id": None, "action": ""}

        with self._state_lock:
            if self._trigger_in_flight:
                return {"executed": False, "status": "BUSY", "order_id": None, "action": ""}
            self._trigger_in_flight = True

        try:
            if action in {"CANCEL_PENDING", "CANCEL_REPLACE"}:
                cancel_status = self._trade_svc.cancel_pending_order(
                    restore_engine_snapshot=self._restore_engine_snapshot,
                )
                if action == "CANCEL_PENDING":
                    return {
                        "executed": cancel_status.status == "CANCELLED",
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_PENDING",
                    }
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return {
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_REPLACE",
                    }
                replacement_action = str(decision.get("replacement_action") or "NONE").upper()
                mapped_action = _LLM_ORDER_ACTION_MAP.get(replacement_action)
                if mapped_action is None:
                    return {
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_REPLACE",
                    }
                return self._execute_llm_trade_action(
                    mapped_action,
                    decision.get("replacement_price") or decision.get("order_price"),
                    allow_loss_exit=replacement_action in _LLM_STOP_LOSS_ACTIONS,
                )

            mapped_action = _LLM_ORDER_ACTION_MAP.get(action)
            if mapped_action is None:
                return {"executed": False, "status": "UNKNOWN_ACTION", "order_id": None, "action": action}
            if self._trade_svc.has_pending_order:
                cancel_status = self._trade_svc.cancel_pending_order(
                    restore_engine_snapshot=self._restore_engine_snapshot,
                )
                replaced_order_id = cancel_status.broker_order_id or None
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return {
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": replaced_order_id,
                        "action": mapped_action,
                    }
                result = self._execute_llm_trade_action(
                    mapped_action,
                    decision.get("order_price"),
                    allow_loss_exit=action in _LLM_STOP_LOSS_ACTIONS,
                )
                if replaced_order_id is not None:
                    result["replaced_order_id"] = replaced_order_id
                return result
            return self._execute_llm_trade_action(
                mapped_action,
                decision.get("order_price"),
                allow_loss_exit=action in _LLM_STOP_LOSS_ACTIONS,
            )
        finally:
            with self._state_lock:
                self._trigger_in_flight = False

    def cancel_order_by_id(self, order_id: str):
        return self._trade_svc.cancel_order_by_id(
            order_id,
            self.broker,
            restore_engine_snapshot=self._restore_engine_snapshot,
        )

    def _execute_llm_trade_action(self, action: str, price: Any = None, *, allow_loss_exit: bool = False) -> dict[str, Any]:
        risk_result = self.risk.check()
        if not risk_result.approved:
            return {"executed": False, "status": "RISK_REJECTED", "order_id": None, "action": action}

        symbol = self.engine.params.symbol
        if not symbol:
            return {"executed": False, "status": "NO_SYMBOL", "order_id": None, "action": action}

        engine_snapshot = self._engine_snapshot()
        state_status = self._set_engine_state_for_order_action(action)
        if state_status != "OK":
            return {"executed": False, "status": state_status, "order_id": None, "action": action}

        quote = self._quote_for_llm_order(symbol, price)
        if quote is None:
            self._restore_engine_snapshot(engine_snapshot)
            return {"executed": False, "status": "NO_QUOTE", "order_id": None, "action": action}

        order_status = self._trade_svc.execute(
            action=action,
            symbol=symbol,
            quote=quote,
            broker=self.broker,
            risk=self.risk,
            notifier=self.notifier,
            cash_currency=self._cash_currency(),
            min_profit_amount=self.engine.params.min_profit_amount,
            allow_loss_exit=allow_loss_exit,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=self._restore_engine_snapshot,
            notify_risk_event=self.notifier.notify_risk_event,
        )
        if order_status is None:
            self._restore_engine_snapshot(engine_snapshot)
            return {"executed": False, "status": "NO_ORDER", "order_id": None, "action": action}
        if order_status.status in {"SKIPPED", "REJECTED", "CANCELLED"}:
            self._restore_engine_snapshot(engine_snapshot)
        return {
            "executed": order_status.status in {"FILLED", "SUBMITTED", "PARTIAL_FILLED"},
            "status": order_status.status,
            "order_id": order_status.broker_order_id or None,
            "action": action,
        }

    def _set_engine_state_for_order_action(self, action: str) -> str:
        with self.engine._lock:
            current = self.engine.state
            if action == "BUY":
                if current != EngineState.FLAT:
                    return "INCOMPATIBLE_STATE"
                self.engine.state = EngineState.LONG
                return "OK"
            if action == "SELL":
                if current != EngineState.LONG:
                    return "INCOMPATIBLE_STATE"
                self.engine.state = EngineState.FLAT
                return "OK"
            if action == "SELL_SHORT":
                if not self.engine.params.short_selling:
                    return "SHORT_SELLING_DISABLED"
                if current != EngineState.FLAT:
                    return "INCOMPATIBLE_STATE"
                self.engine.state = EngineState.SHORT
                return "OK"
            if action == "BUY_TO_COVER":
                if current != EngineState.SHORT:
                    return "INCOMPATIBLE_STATE"
                self.engine.state = EngineState.FLAT
                return "OK"
            return "UNKNOWN_ACTION"

    def _quote_for_llm_order(self, symbol: str, price: Any = None) -> Quote | None:
        override_price = self._coerce_positive_float(price)
        try:
            quote = self.broker.get_quote(symbol)
        except Exception:
            logger.exception("failed to fetch quote for LLM order action")
            fallback_price = override_price or self.engine.last_price
            if fallback_price <= 0:
                return None
            return Quote(symbol=symbol, last_price=fallback_price, bid=fallback_price, ask=fallback_price, timestamp="")
        if override_price > 0:
            return Quote(
                symbol=quote.symbol,
                last_price=override_price,
                bid=quote.bid,
                ask=quote.ask,
                timestamp=quote.timestamp,
            )
        return quote

    @staticmethod
    def _coerce_positive_float(value: Any) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        return result if result > 0 else 0.0

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
            data["last_action_message"] = self.last_action_message
            if self._loop and self._loop.is_running():
                asyncio.run_coroutine_threadsafe(manager.broadcast(data), self._loop)
        except Exception:
            logger.warning("broadcast failed")

    def _set_last_action_message(self, message: str) -> None:
        with self._state_lock:
            self._last_action_message = message

    @property
    def last_action_message(self) -> str:
        with self._state_lock:
            return self._last_action_message

    def _reset_quote_tracking(self, *, clear_history: bool) -> None:
        with self._state_lock:
            self._last_quote_at = 0.0
            self._last_push_quote_at = 0.0
            self._last_active_quote_refresh_at = 0.0
            if clear_history:
                self._recent_quotes = []

    def _remember_quote(self, quote: Quote) -> None:
        now = datetime.now(timezone.utc)
        self._last_quote_at = time.monotonic()
        self._recent_quotes.append(
            {
                "symbol": quote.symbol,
                "last_price": float(quote.last_price),
                "bid": float(quote.bid),
                "ask": float(quote.ask),
                "timestamp": quote.timestamp,
                "observed_at": now,
            }
        )
        cutoff = now - timedelta(seconds=self._recent_quote_window_seconds)
        self._recent_quotes = [
            item
            for item in self._recent_quotes
            if isinstance(item.get("observed_at"), datetime) and item["observed_at"] >= cutoff
        ]
        if len(self._recent_quotes) > self._recent_quotes_cap:
            self._recent_quotes = self._recent_quotes[-self._recent_quotes_cap:]

    def recent_price_context(self, window_seconds: float = 300.0) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=window_seconds)
        with self._state_lock:
            symbol = self.engine.params.symbol
            entries = [
                item
                for item in self._recent_quotes
                if item.get("symbol") == symbol
                and isinstance(item.get("observed_at"), datetime)
                and item["observed_at"] >= cutoff
            ]
            return [
                {
                    "symbol": item["symbol"],
                    "last_price": item["last_price"],
                    "bid": item["bid"],
                    "ask": item["ask"],
                    "timestamp": item.get("timestamp") or "",
                    "observed_at": item["observed_at"].isoformat(),
                }
                for item in entries
            ]

    def _resubscribe_quotes_if_silent(self) -> bool:
        """If the quote stream has been silent past the threshold, drop and resubscribe.

        Active refresh can keep prices current when a stream drops, but it
        intentionally does not update ``_last_push_quote_at``. This watchdog
        therefore repairs a silent subscription even while polling succeeds.
        """
        with self._state_lock:
            if not self._running or self._trigger_in_flight:
                return False
            symbol = self.engine.params.symbol
            if not symbol or not self._quotes_subscribed:
                return False
            if not is_trading_hours(self.engine.params.market):
                return False
            if self._last_push_quote_at <= 0:
                return False
            silence = time.monotonic() - self._last_push_quote_at
            if silence < self._quote_resubscribe_threshold_seconds:
                return False

        try:
            self.broker.unsubscribe_quotes()
        except Exception:
            logger.warning("failed to unsubscribe before resubscribe for %s", symbol)
        try:
            self.broker.subscribe_quotes(symbol, self._on_quote)
        except Exception as exc:
            logger.error("quote resubscribe failed for %s: %s", symbol, exc)
            with self._state_lock:
                self._quotes_subscribed = False
            return False
        with self._state_lock:
            self._quotes_subscribed = True
            self._last_push_quote_at = time.monotonic()
        logger.warning("resubscribed quotes for %s after %.0fs silence", symbol, silence)
        return True

    def _refresh_quote_if_stale(self) -> None:
        with self._state_lock:
            if not self._running or self._trigger_in_flight:
                return
            symbol = self.engine.params.symbol
            if not symbol:
                return
            now = time.monotonic()
            interval = self._active_quote_refresh_interval_seconds
            if self._last_quote_at > 0 and now - self._last_quote_at < interval:
                return
            if (
                self._last_active_quote_refresh_at > 0
                and now - self._last_active_quote_refresh_at < interval
            ):
                return
            self._last_active_quote_refresh_at = now

        try:
            quote = self.broker.get_quote(symbol)
        except Exception as exc:
            logger.warning("active quote refresh failed for %s: %s", symbol, exc)
            return
        self._on_quote(quote, is_push=False)

    def sync_today_orders_from_broker(self, *, force: bool = False) -> int:
        with self._state_lock:
            now = time.monotonic()
            if (
                not force
                and self._last_order_sync_at > 0
                and now - self._last_order_sync_at < self._order_sync_interval_seconds
            ):
                return 0
            self._last_order_sync_at = now

        try:
            broker_orders = self.broker.get_today_orders()
        except Exception as exc:
            logger.warning("broker today order sync failed: %s", exc)
            return 0

        changed = 0
        with self._db_session() as db:
            for broker_order in broker_orders:
                if self._upsert_broker_order(db, broker_order):
                    changed += 1
            if changed:
                db.commit()
        self._sync_risk_from_order_ledger()
        return changed

    def _market_trade_day(self):
        return trade_day_for(self.engine.params.market)

    def _market_trade_day_for(self, instant) -> Any:
        return trade_day_for(self.engine.params.market, instant)

    def _sync_risk_from_order_ledger(self) -> bool:
        try:
            with self._db_session() as db:
                result = DailyPnlService(db).calculate(
                    trade_day=self._market_trade_day(),
                    to_trade_day=self._market_trade_day_for,
                )
        except Exception:
            logger.exception("failed to sync realized daily pnl from order ledger")
            return False

        with self._state_lock:
            old_daily_pnl = self.risk.daily_pnl
            old_consecutive_losses = self.risk.consecutive_losses
            old_daily_pnl_date = self.risk.daily_pnl_date
            changed = (
                abs(old_daily_pnl - result.realized_pnl) > 1e-9
                or old_consecutive_losses != result.consecutive_losses
                or old_daily_pnl_date != result.trade_day
            )
            if not changed:
                return False
            self.risk.replace_daily_pnl(
                result.realized_pnl,
                result.consecutive_losses,
                result.trade_day,
            )

        with self._db_session() as db:
            self._state_svc.persist_risk(db, self.risk)
        logger.info(
            "synced realized daily pnl from order ledger: pnl=%s consecutive_losses=%s trades=%s",
            result.realized_pnl,
            result.consecutive_losses,
            len(result.trades),
        )
        return True

    def _upsert_broker_order(self, db, broker_order: object) -> bool:
        order_id = str(getattr(broker_order, "broker_order_id", "") or "")
        if not order_id:
            return False

        symbol = str(getattr(broker_order, "symbol", "") or "")
        side = str(getattr(broker_order, "side", "") or "")
        status = str(getattr(broker_order, "status", "SUBMITTED") or "SUBMITTED")
        quantity = self._coerce_float(getattr(broker_order, "quantity", 0))
        price = self._coerce_float(getattr(broker_order, "price", 0))
        executed_quantity = self._coerce_optional_float(getattr(broker_order, "executed_quantity", None))
        executed_price = self._coerce_optional_float(getattr(broker_order, "executed_price", None))
        created_at = getattr(broker_order, "created_at", None) or datetime.now(timezone.utc)
        filled_at = getattr(broker_order, "filled_at", None)

        order = (
            db.query(OrderRecord)
            .filter(OrderRecord.broker_order_id == order_id)
            .order_by(OrderRecord.id.desc())
            .first()
        )
        if order is None:
            order = OrderRecord(
                broker_order_id=order_id,
                symbol=symbol,
                side=side,
                quantity=quantity,
                price=price,
                executed_quantity=executed_quantity,
                executed_price=executed_price,
                status=status,
                created_at=created_at,
                filled_at=filled_at,
            )
            db.add(order)
            record_trade_event(
                db,
                event_type="ORDER_SYNCED",
                symbol=symbol,
                broker_order_id=order_id,
                side=side,
                status=status,
                message="broker order discovered during today-order sync",
                payload=self._broker_order_payload(broker_order, old_status=None),
            )
            return True

        old_status = order.status
        old_executed_quantity = order.executed_quantity
        old_executed_price = order.executed_price
        changed = False
        updates = {
            "symbol": symbol,
            "side": side,
            "quantity": quantity,
            "price": price,
            "status": status,
            "executed_quantity": executed_quantity,
            "executed_price": executed_price,
        }
        for name, value in updates.items():
            if getattr(order, name) != value:
                setattr(order, name, value)
                changed = True
        if filled_at is not None and order.filled_at != filled_at:
            order.filled_at = filled_at
            changed = True
        elif status in _TERMINAL_ORDER_STATUSES and order.filled_at is None:
            order.filled_at = datetime.now(timezone.utc)
            changed = True

        if not changed:
            return False

        db.add(order)
        event_type = self._order_event_type_for_status(status)
        if event_type == "ORDER_STATUS_CHANGED" and status == old_status:
            message = f"broker order execution changed while status remained {status}"
        else:
            message = f"broker order status changed from {old_status} to {status}"
        record_trade_event(
            db,
            event_type=event_type,
            symbol=symbol,
            broker_order_id=order_id,
            side=side,
            status=status,
            message=message,
            payload={
                **self._broker_order_payload(broker_order, old_status=old_status),
                "old_executed_quantity": old_executed_quantity,
                "old_executed_price": old_executed_price,
            },
        )
        return True

    @staticmethod
    def _order_event_type_for_status(status: str) -> str:
        if status == "FILLED":
            return "ORDER_FILLED"
        if status == "CANCELLED":
            return "ORDER_CANCELLED"
        if status == "REJECTED":
            return "ORDER_REJECTED"
        return "ORDER_STATUS_CHANGED"

    @staticmethod
    def _coerce_float(value: object) -> float:
        if value is None:
            return 0.0
        try:
            return float(value)  # pyright: ignore[reportArgumentType]
        except Exception:
            return 0.0

    @staticmethod
    def _coerce_optional_float(value: object) -> float | None:
        if value is None:
            return None
        try:
            return float(value)  # pyright: ignore[reportArgumentType]
        except Exception:
            return None

    def _broker_order_payload(self, broker_order: object, *, old_status: str | None) -> dict[str, Any]:
        return {
            "source": "broker_today_order_sync",
            "old_status": old_status,
            "quantity": self._coerce_float(getattr(broker_order, "quantity", 0)),
            "price": self._coerce_float(getattr(broker_order, "price", 0)),
            "executed_quantity": self._coerce_optional_float(getattr(broker_order, "executed_quantity", None)),
            "executed_price": self._coerce_optional_float(getattr(broker_order, "executed_price", None)),
        }

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
                if self.sync_today_orders_from_broker():
                    self._broadcast_status()
            except Exception:
                logger.exception("error syncing broker today orders")
            try:
                self._auto_resume_pause_if_due()
            except Exception:
                logger.exception("error checking pause auto resume")
            try:
                if self._sync_engine_state_with_positions():
                    self._broadcast_status()
            except Exception:
                logger.exception("error syncing engine state with broker positions")
            try:
                self._refresh_quote_if_stale()
            except Exception:
                logger.exception("error refreshing stale quote")
            try:
                self._resubscribe_quotes_if_silent()
            except Exception:
                logger.exception("error resubscribing stale quote stream")
            try:
                with self._db_session() as db:
                    self._state_svc.persist(db, self.engine, self.risk)
            except Exception:
                logger.exception("error persisting state")
            time.sleep(5)

    @staticmethod
    def _is_auto_resumable_pause_reason(reason: str) -> bool:
        normalized = reason.lower()
        transient_markers = (
            "429",
            "rate limit",
            "rate_limit",
            "too many requests",
            "too frequent",
            "throttle",
            "throttled",
            "frequency",
            "限流",
            "频率",
            "请求过于频繁",
        )
        return any(marker in normalized for marker in transient_markers)

    def _sync_engine_state_with_positions(self, *, force: bool = False) -> bool:
        with self._state_lock:
            if not self._running or self._trigger_in_flight or self._trade_svc.has_pending_order:
                return False
            symbol = self.engine.params.symbol
            if not symbol:
                return False
            now = time.monotonic()
            if (
                not force
                and self._last_position_sync_at > 0
                and now - self._last_position_sync_at < self._position_sync_interval_seconds
            ):
                return False
            self._last_position_sync_at = now

        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.warning("position sync failed for %s: %s", symbol, exc)
            return False

        has_long_position = any(
            position.symbol == symbol and position.side == "LONG" and position.quantity > 0
            for position in positions
        )
        has_short_position = any(
            position.symbol == symbol and position.side == "SHORT" and position.quantity > 0
            for position in positions
        )
        if has_long_position:
            desired_state = EngineState.LONG
        elif has_short_position:
            desired_state = EngineState.SHORT
        else:
            desired_state = EngineState.FLAT

        with self._state_lock:
            if self._trigger_in_flight or self._trade_svc.has_pending_order:
                return False
            current_state = self.engine.state
            if current_state == desired_state:
                return False
            self.engine.sync_state(has_long_position, has_short_position)
        logger.info("synced engine state from broker positions: %s -> %s", current_state.value, desired_state.value)
        return True

    def _auto_resume_pause_if_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._state_lock:
            if not self.risk.paused or self.risk.kill_switch:
                return False
            auto_resume_minutes = self.engine.params.auto_resume_minutes
            paused_at = self.risk.paused_at
            if auto_resume_minutes <= 0 or paused_at is None:
                return False
            if paused_at.tzinfo is None:
                paused_at = paused_at.replace(tzinfo=timezone.utc)
            if not self.risk.pause_auto_resumable:
                return False
            if now - paused_at < timedelta(minutes=auto_resume_minutes):
                return False
            reason = self.risk.pause_reason
            self.risk.resume()
        logger.info("auto-resumed trading after transient pause: %s", reason)
        with self._db_session() as db:
            record_trade_event(
                db,
                event_type="RISK_AUTO_RESUMED",
                status="RUNNING",
                message=reason,
                payload={"source": "auto_resume_pause"},
            )
            db.commit()
        self._broadcast_status()
        return True

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
                self._reset_quote_tracking(clear_history=True)
                self._last_push_quote_at = time.monotonic()
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
            record_trade_event(
                db,
                event_type="ORDER_SUBMITTED",
                symbol=symbol,
                broker_order_id=order_id,
                side=side,
                status=status,
                message=f"{side} order submitted",
                payload={"quantity": qty, "price": price, "source": "runner"},
            )
            if status in _TERMINAL_ORDER_STATUSES or status == "PARTIAL_FILLED":
                record_trade_event(
                    db,
                    event_type=self._order_event_type_for_status(status),
                    symbol=symbol,
                    broker_order_id=order_id,
                    side=side,
                    status=status,
                    message=f"{side} order returned immediate status {status}",
                    payload={"quantity": qty, "price": price, "source": "runner_submit_result"},
                )
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
            old_status = order.status
            old_executed_quantity = order.executed_quantity
            old_executed_price = order.executed_price
            order.status = status
            if filled_at is not None:
                order.filled_at = filled_at
            if executed_quantity is not None:
                order.executed_quantity = executed_quantity
            if executed_price is not None:
                order.executed_price = executed_price
            changed = (
                old_status != status
                or old_executed_quantity != order.executed_quantity
                or old_executed_price != order.executed_price
            )
            if changed:
                record_trade_event(
                    db,
                    event_type=self._order_event_type_for_status(status),
                    symbol=order.symbol,
                    broker_order_id=order_id,
                    side=order.side,
                    status=status,
                    message=f"order status changed from {old_status} to {status}",
                    payload={
                        "source": "runner_order_status",
                        "old_status": old_status,
                        "old_executed_quantity": old_executed_quantity,
                        "old_executed_price": old_executed_price,
                        "executed_quantity": order.executed_quantity,
                        "executed_price": order.executed_price,
                    },
                )
            db.add(order)
            db.commit()

    def _record_risk_event(self, reason: str) -> None:
        with self._db_session() as db:
            self._state_svc.record_risk_event(db, reason)
            record_trade_event(
                db,
                event_type="RISK_PAUSED",
                status="PAUSED",
                message=reason,
                payload={"source": "risk_controller"},
            )
            db.commit()

    def _load_tracked_entries(self, db) -> None:
        try:
            rows = db.query(TrackedEntry).all()
        except Exception:
            logger.exception("failed to load tracked entries")
            return
        entries: dict[str, tuple[Decimal, Decimal]] = {}
        for row in rows:
            try:
                quantity = Decimal(str(row.quantity))
                cost = Decimal(str(row.cost))
            except Exception:
                continue
            if quantity > 0 and cost > 0:
                entries[row.symbol] = (quantity, cost)
        if entries:
            self._trade_svc.load_tracked_entries(entries)
            logger.info("restored %d tracked entry positions from db", len(entries))

    def _persist_tracked_entry(self, symbol: str, quantity: Decimal, cost: Decimal) -> None:
        with self._db_session() as db:
            existing = db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).first()
            if quantity <= 0:
                if existing is not None:
                    db.delete(existing)
                    db.commit()
                return
            if existing is None:
                existing = TrackedEntry(
                    symbol=symbol,
                    quantity=float(quantity),
                    cost=float(cost),
                )
                db.add(existing)
            else:
                existing.quantity = float(quantity)
                existing.cost = float(cost)
                existing.updated_at = datetime.now(timezone.utc)
            db.commit()

    def _reconcile_tracked_entries_with_broker(self, db) -> None:
        snapshot = self._trade_svc.snapshot_tracked_entries()
        if not snapshot:
            return
        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.warning("tracked entry reconciliation skipped: %s", exc)
            return
        broker_qty: dict[str, Decimal] = {}
        for pos in positions:
            try:
                qty = Decimal(str(pos.quantity))
            except Exception:
                continue
            broker_qty[pos.symbol] = broker_qty.get(pos.symbol, Decimal("0")) + qty
        for symbol, (tracked_qty, tracked_cost) in snapshot.items():
            broker_have = broker_qty.get(symbol, Decimal("0"))
            if tracked_qty <= 0:
                continue
            drift_pct = abs(tracked_qty - broker_have) / tracked_qty
            if drift_pct < Decimal("0.05") and abs(tracked_qty - broker_have) < Decimal("1"):
                continue
            payload = {
                "symbol": symbol,
                "tracked_quantity": float(tracked_qty),
                "tracked_avg_price": float(tracked_cost / tracked_qty) if tracked_qty > 0 else 0.0,
                "broker_quantity": float(broker_have),
                "drift_pct": float(drift_pct),
                "source": "startup_tracked_entry_reconcile",
            }
            record_trade_event(
                db,
                event_type="TRACKED_ENTRY_DRIFT",
                symbol=symbol,
                status="WARNING",
                message=(
                    f"tracked qty {tracked_qty} diverged from broker qty {broker_have} for {symbol}"
                ),
                payload=payload,
            )
        db.commit()

    def _record_order_skipped(
        self,
        symbol: str,
        side: str,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        with self._db_session() as db:
            record_trade_event(
                db,
                event_type="ORDER_SKIPPED",
                symbol=symbol,
                side=side,
                status="SKIPPED",
                message=reason,
                payload={"source": "trade_precheck", **payload},
            )
            db.commit()


_runner: AppRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AppRunner()
    return _runner
