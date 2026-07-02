from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from collections import deque
from contextlib import contextmanager
from dataclasses import dataclass, replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Deque, Generator, Optional, cast
from sqlalchemy.orm import Session

from app.api.deps import init_audit_logger
from app.api.ws import manager
from app.config import settings
from app.core.audit import AuditLogger
from app.core.broker import BrokerGateway, Quote
from app.core.engine import EngineSnapshot, EngineState, StrategyEngine, StrategyParams, TriggerResult
from app.core.fees import one_side_fee_rate
from app.core.market_calendar import is_trading_hours, trade_day_for
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.core.notifiers.serverchan import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, TrackedEntry
from app.services.daily_pnl_service import DailyPnlService
from app.services.notification_log_service import get_notification_sink
from app.core.credential_crypto import CredentialIntegrityError
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService
from app.services.trade_event_service import record_trade_event
from app.services.trade_execution_service import TradeExecutionService, _PendingOrder
from app.services.watchlist_service import WatchlistService

logger = logging.getLogger("auto_trade.runner")

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
DISCONNECT_RETRY_EXHAUSTED_THRESHOLD = 3

_QUOTE_SPREAD_THRESHOLD_PCT = 0.05  # Reject quotes with >5% bid-ask spread
_POSITION_DRIFT_PCT_TOLERANCE = Decimal("0.05")  # 5% position drift tolerance
_POSITION_DRIFT_SHARE_TOLERANCE = Decimal("1")  # 1 share absolute drift tolerance


@dataclass
class SymbolRuntime:
    symbol: str
    market: str
    engine: StrategyEngine
    recent_quotes: Deque[dict[str, Any]]


@dataclass
class _QuoteTriggerDecision:
    """Carries the result of quote evaluation from the locked section to the unlocked section."""
    processing_started: bool = False
    result: TriggerResult | None = None
    engine_snapshot: EngineSnapshot | None = None
    trigger_symbol: str | None = None
    trigger_engine: StrategyEngine | None = None
    trigger_market: str = ""
    early_return: bool = False


class AppRunner:
    @staticmethod
    def _build_broker(audit: AuditLogger) -> BrokerGateway:
        try:
            return BrokerGateway(audit=audit)
        except TypeError:
            # test doubles may not accept `audit=`
            return BrokerGateway()

    def __init__(self) -> None:
        self._audit: AuditLogger = init_audit_logger()
        self.broker = self._build_broker(self._audit)
        self.engine = StrategyEngine()
        self._symbol_runtimes: dict[str, SymbolRuntime] = {}
        self.risk = RiskController(trade_day_provider=self._market_trade_day)
        self.notifier = MultiChannelNotifier(
            [(ServerChanNotifier(""), "INFO")],
            sink=get_notification_sink().record,
        )
        self._trade_svc = TradeExecutionService(
            record_order=self._record_order,
            update_order_status=self._update_order_status,
            record_risk_event=self._record_risk_event,
            record_order_skipped=self._record_order_skipped,
            persist_entry=self._persist_tracked_entry,
            on_fill=self._mark_fill_processed,
            audit=self._audit,
            margin_safety_factor=None,
        )
        self._state_svc = RuntimeStateService()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._quotes_subscribed = False
        self._disconnect_retry_count = 0
        self._trading_session_mode: str = "ANY"
        self._trigger_in_flight = False
        self._defer_broker_close = False
        self._last_quote_at = 0.0
        self._last_push_quote_at = 0.0
        # Per-pending reconcile tracking. Set just before each pending is
        # reconciled so the restore_engine_snapshot closure can resolve the
        # correct per-symbol engine. Best-effort — see TODO in _run_loop.
        self._current_reconcile_symbol: str | None = None
        self._last_active_quote_refresh_at = 0.0
        self._active_quote_refresh_interval_seconds = 15.0
        self._quote_resubscribe_threshold_seconds = 90.0
        self._last_position_sync_at = 0.0
        self._position_sync_interval_seconds = 15.0
        self._last_order_sync_at = 0.0
        self._order_sync_interval_seconds = 15.0
        self._recent_quote_window_seconds = 300.0
        self._recent_quotes_cap = 500
        self._recent_quotes: Deque[dict[str, Any]] = deque(maxlen=self._recent_quotes_cap)
        self._last_action_message = ""
        self._last_llm_action_at: dict[tuple[str, str], float] = {}
        # Per-symbol last fill timestamp. Previously a single float, which
        # caused a fill on symbol B to skip a position sync on symbol A
        # even though they are unrelated.
        self._last_fill_at: dict[str, float] = {}

    def _mark_fill_processed(self, symbol: str) -> None:
        with self._state_lock:
            fill_symbol = symbol or self.engine.params.symbol
            if fill_symbol:
                self._last_fill_at[fill_symbol] = time.monotonic()
        # Persist immediately on fill so a crash before the 5s snapshot loop
        # does not lose the new engine state, tracked entry, or risk counters.
        # We do this in a best-effort fire-and-forget path because the
        # caller (trade_execution_service._finalize_pending_fill) is already
        # running under tight latency constraints.
        def _persist_async() -> None:
            try:
                with self._db_session() as db:
                    self._state_svc.persist(db, self.engine, self.risk)
                    if fill_symbol:
                        runtime = self._symbol_runtimes.get(fill_symbol)
                        if runtime is not None and runtime.engine is not self.engine:
                            self._state_svc.persist_symbol(db, runtime.engine, fill_symbol)
            except Exception:
                logger.exception("post-fill persist failed for %s", fill_symbol)

        threading.Thread(target=_persist_async, name="post-fill-persist", daemon=True).start()

    def _initialize_runner(self) -> None:
        with self._db_session() as db:
            config = self._state_svc.load(db, self.engine, self.risk)
            self._trade_svc.margin_safety_factor = getattr(config, "margin_safety_factor", None)
            self._load_tracked_entries(db)
            self._sync_symbol_runtimes(db)
            self._apply_credentials(self._load_credentials(), resubscribe=False)
        self._register_broker_disconnect_hook()
        self._refresh_trading_session_mode()

        self.sync_today_orders_from_broker(force=True)
        with self._db_session() as db:
            self._load_pending_orders(db)
        self._sync_risk_from_order_ledger()
        with self._db_session() as db:
            self._pause_if_unresolved_live_order_exists(db)
            self._reconcile_tracked_entries_with_broker(db)
        # Force an engine-vs-broker position sync BEFORE the quote
        # subscription is set up below. Without this, the engine state we
        # just loaded from DB can be stale (positions opened or closed
        # outside the service) for the first ~15s of quote-driven
        # decisions, leading to spurious buys/sells.
        try:
            self._sync_engine_state_with_positions(force=True)
        except Exception:
            logger.exception("initial engine state sync with broker positions failed")

        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            pass

        self._reset_quote_tracking(clear_history=True)
        with self._state_lock:
            symbols = self._desired_quote_symbols_locked()
        if symbols and not self._quotes_subscribed:
            try:
                self._subscribe_quote_symbols(self.broker, symbols)
                self._quotes_subscribed = True
                self._last_push_quote_at = time.monotonic()
                logger.info("subscribed to quote streams: %s", ", ".join(symbols))
            except Exception as exc:
                logger.error("quote subscription failed for %s: %s", ", ".join(symbols), exc)
                logger.error("system running without quote updates")

    def _pause_if_unresolved_live_order_exists(self, db) -> bool:
        order = (
            db.query(OrderRecord)
            .filter(OrderRecord.status.in_(_LIVE_ORDER_STATUSES))
            .order_by(OrderRecord.id.desc())
            .first()
        )
        if order is None:
            return False
        reason = f"unresolved live order {order.broker_order_id} requires manual confirmation"
        logger.warning(reason)
        self.risk.pause(reason)
        try:
            market = getattr(self.engine.params, "market", "")
            record_trade_event(
                db,
                event_type="RISK_PAUSED",
                status="PAUSED",
                message=reason,
                payload={
                    "reason": "unresolved_live_order",
                    "live_order_id": order.broker_order_id,
                    "trade_day": str(trade_day_for(market, datetime.now(timezone.utc))),
                },
            )
            db.commit()
        except Exception as exc:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning("risk_paused_event_record_failed", extra={"err": str(exc)})
        return True

    def _register_broker_disconnect_hook(self) -> None:
        register = getattr(self.broker, "register_disconnect_hook", None)
        if not callable(register):
            return
        register(self._on_disconnect)

    def _desired_quote_symbols_locked(self) -> list[str]:
        primary_symbol = self.engine.params.symbol
        symbols: list[str] = []
        if primary_symbol:
            symbols.append(primary_symbol)
        for symbol in self._symbol_runtimes:
            if symbol and symbol != primary_symbol:
                symbols.append(symbol)
        return symbols

    def _subscribe_quote_symbols(self, broker: Any, symbols: list[str]) -> None:
        for symbol in symbols:
            broker.subscribe_quotes(symbol, self._on_quote)

    def _resubscribe_quote_symbols(self, broker: Any, symbols: list[str]) -> None:
        try:
            broker.unsubscribe_quotes()
        except Exception:
            logger.warning("failed to unsubscribe before resubscribe for %s", ", ".join(symbols))
        self._subscribe_quote_symbols(broker, symbols)

    def _record_broker_retry_exhausted(self, reason: str) -> None:
        try:
            self._audit.record(
                "BROKER_RETRY_EXHAUSTED",
                severity="CRITICAL",
                request_summary={
                    "reason": reason,
                    "disconnect_retry_count": self._disconnect_retry_count,
                },
            )
        except Exception as exc:
            logger.warning("audit_record_failed: %s", exc)

    def _on_disconnect(self, reason: str) -> None:
        """Handle broker SDK disconnect events without auto-pausing trading."""
        reason_text = str(reason)
        logger.warning("broker_disconnect", extra={"reason": reason_text})
        try:
            self._audit.record(
                "BROKER_DISCONNECT",
                severity="WARNING",
                request_summary={"reason": reason_text},
            )
        except Exception as exc:
            logger.warning("audit_record_failed: %s", exc)
        with self._state_lock:
            self._quotes_subscribed = False
            self._disconnect_retry_count += 1
            retry_count = self._disconnect_retry_count
        if retry_count >= DISCONNECT_RETRY_EXHAUSTED_THRESHOLD:
            self._record_broker_retry_exhausted(reason_text)

    def _on_resubscribe_if_needed(self) -> None:
        """Resubscribe quotes after an SDK disconnect marks the stream unsubscribed."""
        with self._state_lock:
            if self._quotes_subscribed or self._trigger_in_flight:
                return
            symbols = self._desired_quote_symbols_locked()
            if not symbols:
                return

        try:
            self._resubscribe_quote_symbols(self.broker, symbols)
        except Exception as exc:
            logger.error("quote resubscribe after disconnect failed for %s: %s", ", ".join(symbols), exc)
            with self._state_lock:
                self._disconnect_retry_count += 1
                retry_count = self._disconnect_retry_count
            if retry_count >= DISCONNECT_RETRY_EXHAUSTED_THRESHOLD:
                self._record_broker_retry_exhausted(str(exc))
            raise
        with self._state_lock:
            self._quotes_subscribed = True
            self._disconnect_retry_count = 0
            self._last_push_quote_at = time.monotonic()

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

    def diagnostics(self) -> dict[str, Any]:
        now = time.monotonic()

        def age_since(value: float) -> float | None:
            if value <= 0:
                return None
            return max(0.0, now - value)

        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            pending_order_symbols = sorted(getattr(self._trade_svc, "_pending_orders", {}).keys())
            primary_symbol = self.engine.params.symbol
            symbol_runtimes = [
                {
                    "symbol": runtime.symbol,
                    "market": runtime.market,
                    "is_primary": symbol == primary_symbol,
                    "engine_state": runtime.engine.state.value,
                    "last_price": float(runtime.engine.last_price),
                    "last_trigger_price": float(runtime.engine.last_trigger_price),
                    "recent_quote_count": len(runtime.recent_quotes),
                    "has_pending_order": self._trade_svc.pending_order_for(symbol) is not None,
                    "quote_quality": self._quote_quality_for_runtime(runtime),
                }
                for symbol, runtime in sorted(self._symbol_runtimes.items())
            ]
            if primary_symbol and primary_symbol not in self._symbol_runtimes:
                symbol_runtimes.insert(
                    0,
                    {
                        "symbol": primary_symbol,
                        "market": self.engine.params.market,
                        "is_primary": True,
                        "engine_state": self.engine.state.value,
                        "last_price": float(self.engine.last_price),
                        "last_trigger_price": float(self.engine.last_trigger_price),
                        "recent_quote_count": len(self._recent_quotes),
                        "has_pending_order": self._trade_svc.pending_order_for(primary_symbol) is not None,
                        "quote_quality": self._quote_quality_for_primary(),
                    },
                )
            return {
                "runner_running": self._running and thread_alive,
                "thread_alive": thread_alive,
                "quotes_subscribed": self._quotes_subscribed,
                "trigger_in_flight": self._trigger_in_flight,
                "pending_order_symbols": pending_order_symbols,
                "quote_stream": {
                    "last_push_age_seconds": age_since(self._last_push_quote_at),
                    "last_quote_age_seconds": age_since(self._last_quote_at),
                    "recent_quote_count": len(self._recent_quotes),
                },
                "risk": {
                    "paused": self.risk.paused,
                    "kill_switch": self.risk.kill_switch,
                    "pause_reason": self.risk.pause_reason,
                    "daily_pnl": float(self.risk.daily_pnl),
                    "consecutive_losses": self.risk.consecutive_losses,
                },
                "symbol_runtimes": symbol_runtimes,
            }

    def llm_symbol_statuses(self) -> list[dict[str, Any]]:
        now = time.monotonic()

        def cooldown_remaining(symbol: str, side: str, seconds: int) -> float | None:
            if seconds <= 0:
                return None
            last_at = self._last_llm_action_at.get((symbol, side))
            if last_at is None:
                return None
            return max(0.0, seconds - (now - last_at))

        with self._state_lock:
            primary_symbol = self.engine.params.symbol
            runtimes: list[tuple[str, str, StrategyEngine]] = []
            seen: set[str] = set()
            for symbol, runtime in sorted(self._symbol_runtimes.items()):
                runtimes.append((symbol, runtime.market, runtime.engine))
                seen.add(symbol)
            if primary_symbol and primary_symbol not in seen:
                runtimes.insert(0, (primary_symbol, self.engine.params.market, self.engine))

            return [
                {
                    "symbol": symbol,
                    "market": market,
                    "is_primary": symbol == primary_symbol,
                    "has_pending_order": self._trade_svc.pending_order_for(symbol) is not None,
                    "buy_cooldown_remaining_seconds": cooldown_remaining(
                        symbol,
                        "BUY",
                        engine.params.llm_action_cooldown_seconds,
                    ),
                    "sell_cooldown_remaining_seconds": cooldown_remaining(
                        symbol,
                        "SELL",
                        engine.params.llm_action_cooldown_seconds,
                    ),
                }
                for symbol, market, engine in runtimes
            ]

    def stop(self) -> None:
        defer_broker_close = False
        with self._state_lock:
            self._running = False
            self._quotes_subscribed = False
            if self._trigger_in_flight:
                self._defer_broker_close = True
                defer_broker_close = True
        # The trigger thread is the only writer of ``_trigger_in_flight``,
        # but if the broker SDK call is blocked we may never observe it flip
        # back. Wait the polite timeout first; if the thread is still alive
        # the broker is hung, so we have to fall back to force-closing.
        trigger_thread = self._thread
        if trigger_thread is not None and trigger_thread.is_alive():
            trigger_thread.join(timeout=10)
            if trigger_thread.is_alive():
                logger.warning(
                    "trigger thread did not exit within 10s of stop(); "
                    "broker is likely blocked — forcing broker.close()"
                )
                with self._state_lock:
                    self._trigger_in_flight = False
                defer_broker_close = False
        if defer_broker_close:
            return
        try:
            self.broker.close()
        except Exception as exc:
            logger.warning("broker.close() during stop raised: %s", exc)

    def reload_credentials(self) -> None:
        try:
            credentials = self._load_credentials()
        except CredentialIntegrityError as exc:
            logger.error(
                "skipping credential reload: %s — broker/notifier unchanged",
                exc,
            )
            return
        self._apply_credentials(credentials, resubscribe=self._running)

    def reload_strategy(self) -> None:
        db = SessionLocal()
        try:
            svc = StrategyService(db)
            config = svc.get_config()
            new_params = StrategyParams(
                symbol=config.symbol,
                market=config.market,
                buy_low=config.buy_low,
                sell_high=config.sell_high,
                short_selling=config.short_selling,
                min_profit_amount=config.min_profit_amount,
                auto_resume_minutes=config.auto_resume_minutes,
                fee_rate_us=config.fee_rate_us,
                fee_rate_hk=config.fee_rate_hk,
                min_repricing_pct=config.min_repricing_pct,
                llm_action_cooldown_seconds=config.llm_action_cooldown_seconds,
            )
            new_risk_config = RiskConfig(
                max_daily_loss=config.max_daily_loss,
                max_consecutive_losses=config.max_consecutive_losses,
            )
            mode = getattr(config, "trading_session_mode", None)
            new_session_mode = mode if mode else "ANY"
            new_margin_safety_factor = getattr(config, "margin_safety_factor", None)

            need_resubscribe = False
            with self._state_lock:
                self.engine.params = new_params
                self.risk.config = new_risk_config
                self._trading_session_mode = new_session_mode
                self._trade_svc.margin_safety_factor = new_margin_safety_factor
                self._sync_symbol_runtimes(db)
                if self._running:
                    self._reset_quote_tracking(clear_history=True)
                    if self._quotes_subscribed:
                        need_resubscribe = True
                        self._quotes_subscribed = False

            if need_resubscribe:
                try:
                    self.broker.unsubscribe_quotes()
                except Exception:
                    logger.warning("failed to unsubscribe old symbols during strategy reload")
                with self._state_lock:
                    symbols = self._desired_quote_symbols_locked()
                if symbols:
                    try:
                        self._subscribe_quote_symbols(self.broker, symbols)
                        with self._state_lock:
                            self._quotes_subscribed = True
                            self._last_push_quote_at = time.monotonic()
                        logger.info("re-subscribed to quote streams after strategy reload: %s", ", ".join(symbols))
                    except Exception as exc:
                        logger.error("quote subscription failed after strategy reload: %s", exc)
        finally:
            db.close()

    def _evaluate_quote_trigger(self, quote: Quote, *, is_push: bool = True) -> _QuoteTriggerDecision:
        """Evaluate a quote under state lock; returns trigger decision."""
        decision = _QuoteTriggerDecision()
        with self._state_lock:
            if is_push:
                self._last_push_quote_at = time.monotonic()
            self._remember_quote(quote)
            runtime = self._symbol_runtimes.get(quote.symbol)
            is_primary_symbol = quote.symbol == self.engine.params.symbol
            if runtime is None and not is_primary_symbol:
                decision.early_return = True
                logger.debug(
                    "ignoring quote for unrecognised symbol %s (no symbol runtime registered)",
                    quote.symbol,
                )
                return decision
            if is_primary_symbol:
                active_engine: StrategyEngine = self.engine
                active_market: str = self.engine.params.market
            else:
                # runtime is non-None here: the early-return above already
                # excluded the case where the symbol has no registered runtime.
                active_engine = cast("SymbolRuntime", runtime).engine
                active_market = cast("SymbolRuntime", runtime).market
            if not self._running or self._trigger_in_flight:
                decision.early_return = True
                return decision
            if self._trade_svc.pending_order_for(quote.symbol) is not None:
                self._trigger_in_flight = True
                decision.processing_started = True
            elif not self.risk.check().approved:
                active_engine.record_price(quote.last_price)
            else:
                decision.engine_snapshot = active_engine.snapshot()
                decision.result = active_engine.update_price(quote.last_price)
                if decision.result.triggered:
                    decision.trigger_symbol = active_engine.params.symbol or quote.symbol
                    decision.trigger_engine = active_engine
                    decision.trigger_market = active_market
                    self._trigger_in_flight = True
                    decision.processing_started = True
        return decision

    def _execute_triggered_order(
        self,
        decision: _QuoteTriggerDecision,
        quote: Quote,
    ) -> None:
        """Execute an order after a trigger, handle results and errors."""
        result = decision.result
        trigger_engine = decision.trigger_engine
        if result is None or not result.triggered or trigger_engine is None:
            return
        self._set_last_action_message(result.description)

        restore_engine_snapshot = lambda snapshot, eng=trigger_engine: eng.restore(snapshot)
        restore_engine_state_preserve_trigger = (
            lambda snapshot, eng=trigger_engine: eng.restore_preserving_trigger(snapshot)
        )
        engine_snapshot = decision.engine_snapshot
        risk_result = self.risk.check()
        if not risk_result.approved:
            logger.warning("risk rejected: %s", risk_result.reason)
            self._set_last_action_message(f"{result.action} rejected by risk: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            if engine_snapshot is not None:
                restore_engine_snapshot(engine_snapshot)
            self._broadcast_status()
            return

        try:
            order_status = self._trade_svc.execute(
                action=result.action,
                symbol=decision.trigger_symbol or quote.symbol,
                quote=quote,
                broker=self.broker,
                risk=self.risk,
                notifier=self.notifier,
                cash_currency=self._cash_currency_for_market(decision.trigger_market),
                market=decision.trigger_market,
                trading_session_mode=self._get_trading_session_mode(),
                min_profit_amount=trigger_engine.params.min_profit_amount,
                fee_rate=self._live_fee_rate_for_market(decision.trigger_market),
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=self.notifier.notify_risk_event,
            )
            if order_status is None:
                self._set_last_action_message(f"{result.action} skipped: no order submitted")
                if engine_snapshot is not None:
                    restore_engine_snapshot(engine_snapshot)
            elif order_status.status == "SKIPPED":
                reason = order_status.reason or "order skipped"
                self._set_last_action_message(f"{result.action} skipped: {reason}")
                if engine_snapshot is not None:
                    restore_engine_state_preserve_trigger(engine_snapshot)
            elif order_status.status in {"REJECTED", "CANCELLED"}:
                self._set_last_action_message(f"{result.action} ended with status {order_status.status}")
                if engine_snapshot is not None:
                    restore_engine_snapshot(engine_snapshot)
            else:
                order_id = order_status.broker_order_id or "-"
                self._set_last_action_message(f"{result.action} {order_status.status}: {order_id}")
                # Record fill timestamp to protect position sync from stale snapshots
                if order_status.status in {"FILLED", "SUBMITTED", "PARTIAL_FILLED"}:
                    self._mark_fill_processed(symbol=decision.trigger_symbol or quote.symbol)
            self._broadcast_status()
        except Exception as exc:
            if engine_snapshot is not None:
                restore_engine_snapshot(engine_snapshot)
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

    def _on_quote(self, quote: Quote, *, is_push: bool = True) -> None:
        processing_started = False
        try:
            decision = self._evaluate_quote_trigger(quote, is_push=is_push)
            processing_started = decision.processing_started

            if decision.early_return:
                return

            if self._trade_svc.has_pending_order and processing_started and decision.result is None:
                # Each _PendingOrder already carries its own
                # ``restore_engine_snapshot_fn`` bound to the per-symbol
                # engine at track time. We only need a fallback for any
                # pending that lacked that binding — that restores the
                # primary engine. The single call to ``reconcile()``
                # iterates ALL pending orders internally; wrapping it in
                # a runner-level loop would be a double-iteration and
                # would also misattribute the restore callback across
                # symbols (the prior _drive_reconcile_per_pending helper
                # had this exact bug — see review 2026-06-14).
                def _fallback_restore(snapshot):
                    self.engine.restore(snapshot)

                self._trade_svc.reconcile(
                    self.risk,
                    self.notifier,
                    _fallback_restore,
                    self.notifier.notify_risk_event,
                )
                self._broadcast_status()
                return

            self._broadcast_status()
            self._execute_triggered_order(decision, quote)
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

    def _runtime_for_symbol(self, symbol: str | None) -> tuple[str, str, StrategyEngine] | None:
        requested_symbol = str(symbol or "").upper()
        if not requested_symbol or requested_symbol == self.engine.params.symbol:
            return self.engine.params.symbol, self.engine.params.market, self.engine
        runtime = self._symbol_runtimes.get(requested_symbol)
        if runtime is None:
            return None
        return runtime.symbol, runtime.market, runtime.engine

    def execute_llm_order_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        result = self._execute_llm_order_decision(decision)
        self._record_llm_order_result(result)
        return result

    def _execute_llm_order_decision(self, decision: dict[str, Any]) -> dict[str, Any]:
        action = str(decision.get("order_action") or "NONE").upper()
        if action == "NONE":
            return {"executed": False, "status": "NO_ACTION", "order_id": None, "action": ""}
        if not self._running:
            return {"executed": False, "status": "RUNNER_STOPPED", "order_id": None, "action": ""}

        runtime = self._runtime_for_symbol(decision.get("symbol") if isinstance(decision.get("symbol"), str) else None)
        if runtime is None:
            return {"executed": False, "status": "UNKNOWN_SYMBOL", "order_id": None, "action": ""}
        target_symbol, target_market, target_engine = runtime

        with self._state_lock:
            if self._trigger_in_flight:
                return {"executed": False, "status": "BUSY", "order_id": None, "action": ""}
            self._trigger_in_flight = True

        try:
            if action in {"CANCEL_PENDING", "CANCEL_REPLACE"}:
                if action == "CANCEL_PENDING":
                    cancel_status = self._trade_svc.cancel_pending_order_for_symbol(
                        target_symbol,
                        risk=self.risk,
                        notifier=self.notifier,
                        restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
                        notify_risk_event=self.notifier.notify_risk_event,
                    )
                    return {
                        "executed": cancel_status.status == "CANCELLED",
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_PENDING",
                    }
                replacement_action = str(decision.get("replacement_action") or "NONE").upper()
                mapped_action = _LLM_ORDER_ACTION_MAP.get(replacement_action)
                if mapped_action is None:
                    return {"executed": False, "status": "UNKNOWN_ACTION", "order_id": None, "action": "CANCEL_REPLACE"}
                proposed_price = decision.get("replacement_price") or decision.get("order_price")
                pending = self._trade_svc.pending_order_for(target_symbol)
                skipped = self._precheck_llm_action(
                    mapped_action,
                    proposed_price,
                    pending,
                    symbol=target_symbol,
                    engine=target_engine,
                )
                if skipped is not None:
                    return skipped
                session_skipped = self._check_trading_session(
                    mapped_action, symbol=target_symbol, market=target_market
                )
                if session_skipped is not None:
                    return session_skipped
                cancel_status = self._trade_svc.cancel_pending_order_for_symbol(
                    target_symbol,
                    risk=self.risk,
                    notifier=self.notifier,
                    restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
                    notify_risk_event=self.notifier.notify_risk_event,
                )
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return {
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_REPLACE",
                    }
                return self._execute_llm_trade_action(
                    mapped_action,
                    proposed_price,
                    allow_loss_exit=replacement_action in _LLM_STOP_LOSS_ACTIONS,
                    symbol=target_symbol,
                    market=target_market,
                    engine=target_engine,
                )

            mapped_action = _LLM_ORDER_ACTION_MAP.get(action)
            if mapped_action is None:
                return {"executed": False, "status": "UNKNOWN_ACTION", "order_id": None, "action": action}
            pending = self._trade_svc.pending_order_for(target_symbol)
            skipped = self._precheck_llm_action(
                mapped_action,
                decision.get("order_price"),
                pending,
                symbol=target_symbol,
                engine=target_engine,
            )
            if skipped is not None:
                return skipped
            if pending is not None:
                session_skipped = self._check_trading_session(
                    mapped_action, symbol=target_symbol, market=target_market
                )
                if session_skipped is not None:
                    return session_skipped
                cancel_status = self._trade_svc.cancel_pending_order_for_symbol(
                    target_symbol,
                    risk=self.risk,
                    notifier=self.notifier,
                    restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
                    notify_risk_event=self.notifier.notify_risk_event,
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
                    symbol=target_symbol,
                    market=target_market,
                    engine=target_engine,
                )
                if replaced_order_id is not None:
                    result["replaced_order_id"] = replaced_order_id
                return result
            return self._execute_llm_trade_action(
                mapped_action,
                decision.get("order_price"),
                allow_loss_exit=action in _LLM_STOP_LOSS_ACTIONS,
                symbol=target_symbol,
                market=target_market,
                engine=target_engine,
            )
        finally:
            with self._state_lock:
                self._trigger_in_flight = False

    def cancel_order_by_id(self, order_id: str):
        pending = self._trade_svc.pending_order_by_broker_id(order_id)
        restore_fn = self.engine.restore
        if pending is not None:
            runtime = self._runtime_for_symbol(pending.symbol)
            if runtime is not None:
                _, _, target_engine = runtime
                restore_fn = lambda snapshot, eng=target_engine: eng.restore(snapshot)
        return self._trade_svc.cancel_order_by_id(
            order_id,
            self.broker,
            risk=self.risk,
            notifier=self.notifier,
            restore_engine_snapshot=restore_fn,
            notify_risk_event=self.notifier.notify_risk_event,
        )

    def _execute_llm_trade_action(
        self,
        action: str,
        price: Any = None,
        *,
        allow_loss_exit: bool = False,
        symbol: str | None = None,
        market: str | None = None,
        engine: StrategyEngine | None = None,
    ) -> dict[str, Any]:
        risk_result = self.risk.check()
        if not risk_result.approved:
            return {"executed": False, "status": "RISK_REJECTED", "order_id": None, "action": action}

        if symbol is not None and engine is not None:
            target_symbol = symbol
            target_market = market or self.engine.params.market
            target_engine = engine
        else:
            runtime = self._runtime_for_symbol(symbol)
            if runtime is None:
                return {"executed": False, "status": "UNKNOWN_SYMBOL", "order_id": None, "action": action}
            target_symbol, target_market, target_engine = runtime
        if not target_symbol:
            return {"executed": False, "status": "NO_SYMBOL", "order_id": None, "action": action}

        engine_snapshot = target_engine.snapshot()
        state_status = target_engine.transition_for_action(action)
        if state_status != "OK":
            return {"executed": False, "status": state_status, "order_id": None, "action": action}

        quote = self._quote_for_llm_order(target_symbol, price)
        if quote is None:
            target_engine.restore(engine_snapshot)
            return {"executed": False, "status": "NO_QUOTE", "order_id": None, "action": action}

        order_status = self._trade_svc.execute(
            action=action,
            symbol=target_symbol,
            quote=quote,
            broker=self.broker,
            risk=self.risk,
            notifier=self.notifier,
            cash_currency=self._cash_currency_for_market(target_market),
            market=target_market,
            trading_session_mode=self._get_trading_session_mode(),
            min_profit_amount=self.engine.params.min_profit_amount,
            allow_loss_exit=allow_loss_exit,
            fee_rate=self._live_fee_rate_for_market(target_market),
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
            notify_risk_event=self.notifier.notify_risk_event,
        )
        if order_status is None:
            target_engine.restore(engine_snapshot)
            return {"executed": False, "status": "NO_ORDER", "order_id": None, "action": action}
        if order_status.status in {"SKIPPED", "REJECTED", "CANCELLED"}:
            target_engine.restore(engine_snapshot)
        result = {
            "executed": order_status.status in {"FILLED", "SUBMITTED", "PARTIAL_FILLED"},
            "status": order_status.status,
            "order_id": order_status.broker_order_id or None,
            "action": action,
        }
        if result["executed"]:
            side = self._broker_side_for_action(action)
            self._last_llm_action_at[(target_symbol, side)] = time.monotonic()
            self._mark_fill_processed(symbol=target_symbol)
        return result

    def _record_llm_order_result(self, result: dict[str, Any]) -> None:
        action = str(result.get("action") or "").upper()
        if not action:
            return
        status = str(result.get("status") or "UNKNOWN").upper()
        order_id = result.get("order_id")
        if order_id:
            message = f"LLM {action} {status}: {order_id}"
        else:
            message = f"LLM {action} {status}"
        self._set_last_action_message(message)
        self._broadcast_status()

    def _quote_for_llm_order(self, symbol: str, price: Any = None) -> Quote | None:
        override_price = self._coerce_positive_float(price)
        try:
            # Use the batch path even for one symbol so the broker round-trip
            # shares its retry/quote context with other quote consumers.
            quotes = self.broker.get_quotes([symbol])
            quote = quotes[0] if quotes else None
        except Exception:
            logger.exception("failed to fetch quote for LLM order action")
            runtime = self._runtime_for_symbol(symbol)
            if runtime is None:
                return None
            _, _, target_engine = runtime
            fallback_price = override_price or target_engine.last_price
            if fallback_price <= 0:
                return None
            return Quote(symbol=symbol, last_price=fallback_price, bid=fallback_price, ask=fallback_price, timestamp="")
        if override_price > 0:
            if quote is None:
                return Quote(
                    symbol=symbol,
                    last_price=override_price,
                    bid=override_price,
                    ask=override_price,
                    timestamp="",
                )
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
        return self._cash_currency_for_market(self.engine.params.market)

    @staticmethod
    def _cash_currency_for_market(market: str) -> str:
        return "HKD" if market == "HK" else "USD"

    def _live_fee_rate(self) -> Decimal:
        return self._live_fee_rate_for_market(self.engine.params.market)

    def _live_fee_rate_for_market(self, market: str) -> Decimal:
        return one_side_fee_rate(
            market,
            Decimal(str(self.engine.params.fee_rate_us or 0)),
            Decimal(str(self.engine.params.fee_rate_hk or 0)),
        )

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
            data["trading_session_mode"] = self._get_trading_session_mode()
            data["is_trading_hours"] = is_trading_hours(self.engine.params.market)
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
                self._recent_quotes = deque(maxlen=self._recent_quotes_cap)

    def _remember_quote(self, quote: Quote) -> None:
        now = datetime.now(timezone.utc)
        self._remember_symbol_runtime_quote(quote, now)
        self._last_quote_at = time.monotonic()
        last_price = float(quote.last_price)
        bid = float(quote.bid)
        ask = float(quote.ask)
        self._recent_quotes.append(
            {
                "symbol": quote.symbol,
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
                "timestamp": quote.timestamp,
                "observed_at": now,
            }
        )
        # Sliding-window prune: deque is already capped by maxlen, so the
        # only remaining work is dropping entries older than the window.
        # We popleft until the head is fresh — amortised O(1) when traffic
        # is steady (most calls do zero pops).
        cutoff = now - timedelta(seconds=self._recent_quote_window_seconds)
        recent = self._recent_quotes
        while recent and isinstance(recent[0].get("observed_at"), datetime) and recent[0]["observed_at"] < cutoff:
            recent.popleft()
        quality = self._evaluate_quote_quality(
            {
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
            }
        )
        if quality["has_quote"] and not quality["price_positive"]:
            logger.warning(
                "quote_quality: non-positive price for %s: last=%s bid=%s ask=%s",
                quote.symbol,
                quote.last_price,
                quote.bid,
                quote.ask,
            )
        elif (
            quality["has_quote"]
            and float(quote.bid) > 0
            and float(quote.ask) > 0
            and not quality["spread_reasonable"]
        ):
            logger.warning(
                "quote_quality: wide spread for %s: last=%s bid=%s ask=%s",
                quote.symbol,
                quote.last_price,
                quote.bid,
                quote.ask,
            )

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
            ]  # _recent_quotes is a deque; linear scan is fine for n ≪ 500
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

    def _quote_quality_for_runtime(self, runtime: SymbolRuntime) -> dict[str, Any]:
        recent = runtime.recent_quotes[-1] if runtime.recent_quotes else None
        return self._evaluate_quote_quality(recent)

    def _quote_quality_for_primary(self) -> dict[str, Any]:
        recent = self._recent_quotes[-1] if self._recent_quotes else None
        return self._evaluate_quote_quality(recent)

    @staticmethod
    def _evaluate_quote_quality(recent: dict[str, Any] | None) -> dict[str, Any]:
        if recent is None:
            return {"has_quote": False, "price_positive": False, "spread_reasonable": False}
        last_price = float(recent.get("last_price", 0))
        bid = float(recent.get("bid", 0))
        ask = float(recent.get("ask", 0))
        price_positive = last_price > 0
        spread_reasonable = False
        if price_positive and bid > 0 and ask > 0 and ask >= bid:
            spread_pct = (ask - bid) / last_price
            spread_reasonable = spread_pct < _QUOTE_SPREAD_THRESHOLD_PCT
        return {
            "has_quote": True,
            "price_positive": price_positive,
            "spread_reasonable": spread_reasonable,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
        }

    def _resubscribe_quotes_if_silent(self) -> bool:
        """If the quote stream has been silent past the threshold, drop and resubscribe.

        Active refresh can keep prices current when a stream drops, but it
        intentionally does not update ``_last_push_quote_at``. This watchdog
        therefore repairs a silent subscription even while polling succeeds.
        """
        with self._state_lock:
            if not self._running or self._trigger_in_flight:
                return False
            symbols = self._desired_quote_symbols_locked()
            if not symbols or not self._quotes_subscribed:
                return False
            if self._last_push_quote_at <= 0:
                return False
            silence = time.monotonic() - self._last_push_quote_at
            if silence < self._quote_resubscribe_threshold_seconds:
                return False
            # Multi-market guard: only resubscribe symbols whose market is
            # currently in trading hours. The previous implementation
            # short-circuited on the primary market only, which meant a
            # silent US stream would skip resubscribe on a busy HK symbol
            # (or vice versa).
            from app.core.market_calendar import market_for_symbol

            in_session_symbols: list[str] = []
            for sym in symbols:
                market = market_for_symbol(sym) or self.engine.params.market
                if is_trading_hours(market):
                    in_session_symbols.append(sym)
            if not in_session_symbols:
                return False

        try:
            self._resubscribe_quote_symbols(self.broker, in_session_symbols)
        except Exception as exc:
            logger.error(
                "quote resubscribe failed for %s: %s",
                ", ".join(in_session_symbols),
                exc,
            )
            with self._state_lock:
                self._quotes_subscribed = False
            return False
        with self._state_lock:
            self._quotes_subscribed = True
            self._last_push_quote_at = time.monotonic()
        logger.warning(
            "resubscribed quotes for %s after %.0fs silence",
            ", ".join(in_session_symbols),
            silence,
        )
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
            quotes = self.broker.get_quotes([symbol])
        except Exception as exc:
            logger.warning("active quote refresh failed for %s: %s", symbol, exc)
            return
        if not quotes:
            return
        self._on_quote(quotes[0], is_push=False)

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
            self._load_pending_orders(db)
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

            new_pnl = result.realized_pnl
            new_losses = result.consecutive_losses
            same_trade_day = old_daily_pnl_date == result.trade_day
            optimistic_replay = new_pnl > old_daily_pnl + 1e-9 or new_losses < old_consecutive_losses
            if same_trade_day and not result.trades and optimistic_replay:
                logger.warning(
                    "ledger replay produced no matched trades and would overwrite risk state "
                    "pnl=%s/losses=%s with pnl=%s/losses=%s; keeping current risk state",
                    old_daily_pnl,
                    old_consecutive_losses,
                    new_pnl,
                    new_losses,
                )
                new_pnl = old_daily_pnl
                new_losses = old_consecutive_losses

            self.risk.replace_daily_pnl(
                new_pnl,
                new_losses,
                result.trade_day,
            )

        with self._db_session() as db:
            self._state_svc.persist_risk(db, self.risk, symbol=self.engine.params.symbol)
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
        preserved_side = order.side
        if preserved_side in {"SELL_SHORT", "BUY_TO_COVER"} and side in {"SELL", "BUY"}:
            sync_side = preserved_side
        else:
            sync_side = side
        updates = {
            "symbol": symbol,
            "side": sync_side,
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
        elif status == "FILLED" and order.filled_at is None:
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
        except (TypeError, ValueError):
            logger.debug("_coerce_optional_float failed for value %r", value)
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
                self._on_resubscribe_if_needed()
            except Exception:
                logger.exception("error resubscribing after broker disconnect")
            try:
                if self._trade_svc.has_pending_order:
                    # Each _PendingOrder already carries its own
                    # ``restore_engine_snapshot_fn`` bound to the per-symbol
                    # engine at track time. We only need a fallback for any
                    # pending that lacked that binding — that restores the
                    # primary engine. The single call to ``reconcile()``
                    # iterates ALL pending orders internally; wrapping it in
                    # a runner-level loop would be a double-iteration and
                    # would also misattribute the restore callback across
                    # symbols (the prior _drive_reconcile_per_pending helper
                    # had this exact bug — see review 2026-06-14).
                    def _fallback_restore(snapshot):
                        self.engine.restore(snapshot)

                    self._trade_svc.reconcile(
                        self.risk,
                        self.notifier,
                        _fallback_restore,
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
                    with self._state_lock:
                        secondary_runtimes = [
                            runtime
                            for symbol, runtime in self._symbol_runtimes.items()
                            if symbol != self.engine.params.symbol
                        ]
                    for runtime in secondary_runtimes:
                        self._state_svc.persist_symbol(db, runtime.engine, runtime.symbol)
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
            # Network/broker transient timeouts. The bare word "timeout" is
            # intentionally NOT included here — too many persistence/lifecycle
            # pauses contain that substring (e.g. ``order status timeout``,
            # ``pending order timed out``) and must NOT auto-resume.
            "rate limit timeout",
            "broker timeout",
            "request timeout",
            "read timeout",
            "connect timeout",
        )
        return any(marker in normalized for marker in transient_markers)

    def _sync_engine_state_with_positions(self, *, force: bool = False) -> bool:
        with self._state_lock:
            if not self._running or self._trigger_in_flight:
                return False
            now = time.monotonic()
            if (
                not force
                and self._last_position_sync_at > 0
                and now - self._last_position_sync_at < self._position_sync_interval_seconds
            ):
                return False

            targets: list[tuple[str, StrategyEngine]] = []
            primary_symbol = self.engine.params.symbol
            if primary_symbol and self._trade_svc.pending_order_for(primary_symbol) is None:
                targets.append((primary_symbol, self.engine))
            for symbol, runtime in self._symbol_runtimes.items():
                if symbol == primary_symbol:
                    continue
                if self._trade_svc.pending_order_for(symbol) is not None:
                    continue
                targets.append((symbol, runtime.engine))
            if not targets:
                return False

            self._last_position_sync_at = now
            snapshot_time = now

        try:
            positions = self.broker.get_positions()
        except Exception as exc:
            logger.warning("position sync failed: %s", exc)
            return False

        any_changed = False
        for symbol, engine in targets:
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
                if self._trigger_in_flight or self._trade_svc.pending_order_for(symbol) is not None:
                    continue
                last_fill_at = self._last_fill_at.get(symbol, 0.0)
                if snapshot_time < last_fill_at:
                    logger.info(
                        "position sync skipped for %s: fill at %.3f is newer than snapshot at %.3f",
                        symbol,
                        last_fill_at,
                        snapshot_time,
                    )
                    continue
                current_state = engine.state
                if current_state == desired_state:
                    continue
                engine.sync_state(has_long_position, has_short_position)
                any_changed = True
                logger.info(
                    "synced engine state from broker positions for %s: %s -> %s",
                    symbol,
                    current_state.value,
                    desired_state.value,
                )
        return any_changed

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
    def _db_session() -> Generator[Session, None, None]:
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    def _load_credentials(self) -> PlainCredentials:
        with self._db_session() as db:
            try:
                return CredentialsService(db).get_plain_credentials()
            except CredentialIntegrityError as exc:
                logger.error(
                    "credential integrity check failed: %s — refusing to apply "
                    "credentials until the master key is restored",
                    exc,
                )
                raise

    def _apply_credentials(self, credentials: PlainCredentials, *, resubscribe: bool) -> None:
        # DESIGN LIMITATION: this method swaps the broker and notifier while
        # in-flight pending orders may still be tracked against the previous
        # broker. Calling this while a pending order is open is unsafe — the
        # reconciliation loop in ``_run_loop`` will issue broker calls on a
        # gateway that no longer matches the order's submit broker. The
        # ``refresh_pending_brokers`` call below rebinds the pending's broker
        # reference to the new gateway, but the order ID semantics may not
        # match. Callers should only invoke this from admin paths when there
        # is no pending order. ``reload_credentials`` is the supported path.
        with self._state_lock:
            if self._trade_svc.has_pending_order:
                pending_symbols = sorted(
                    pending.broker_order_id
                    for pending in self._trade_svc._pending_orders.values()
                    if pending.broker_order_id
                )
                logger.warning(
                    "applying credentials while pending orders are tracked: %s — "
                    "this is a design limitation; ensure the broker switch is safe",
                    ", ".join(pending_symbols),
                )
            symbol = self.engine.params.symbol
            should_resubscribe = resubscribe and bool(symbol)
            sct_key = credentials.sct_key if credentials.sct_key else settings.sct_key
            effective_credentials = (
                credentials if credentials.sct_key == sct_key
                else dataclass_replace(credentials, sct_key=sct_key)
            )
            new_notifier = MultiChannelNotifier.from_credential_config(
                effective_credentials,
                sink=get_notification_sink().record,
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

            new_broker = self._build_broker(self._audit)
            register = getattr(new_broker, "register_disconnect_hook", None)
            if callable(register):
                register(self._on_disconnect)

            if should_resubscribe:
                symbols = self._desired_quote_symbols_locked()
                try:
                    self._subscribe_quote_symbols(new_broker, symbols)
                except Exception as exc:
                    logger.warning("cannot subscribe quotes after credential reload: %s", exc)
                    new_broker.close()
                    return
            old_broker = self.broker
            old_notifier = self.notifier
            self.broker = new_broker
            self.notifier = new_notifier
            self._trade_svc.refresh_pending_brokers(new_broker)
            try:
                old_broker.close()
            except Exception as exc:
                logger.warning("error closing previous broker: %s", exc)
            _close_notifier = getattr(old_notifier, "close", None)
            if callable(_close_notifier):
                try:
                    _close_notifier()
                except Exception as exc:
                    logger.warning("error closing previous notifier: %s", exc)

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

    def _load_pending_orders(self, db) -> None:
        try:
            rows = (
                db.query(OrderRecord)
                .filter(OrderRecord.status.in_(_LIVE_ORDER_STATUSES))
                .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
                .all()
            )
        except Exception:
            logger.exception("failed to load pending orders")
            return

        now = time.monotonic()
        current_utc = datetime.now(timezone.utc)
        pending_orders: list[_PendingOrder] = []
        for row in rows:
            try:
                quantity = Decimal(str(row.quantity))
                price = Decimal(str(row.price))
            except Exception:
                continue
            if quantity <= 0 or price <= 0:
                continue
            submitted_age_seconds = 0.0
            if row.created_at is not None:
                try:
                    submitted_age_seconds = max(0.0, (current_utc - row.created_at).total_seconds())
                except Exception:
                    submitted_age_seconds = 0.0
            pending_orders.append(
                _PendingOrder(
                    broker=self.broker,
                    broker_order_id=row.broker_order_id,
                    symbol=row.symbol,
                    action=row.side,
                    quantity=quantity,
                    price=price,
                    engine_snapshot=None,
                    avg_price=None,
                    next_status_check_at=0.0,
                    submitted_at=now - submitted_age_seconds,
                )
            )
        self._trade_svc.load_pending_orders(pending_orders)
        if pending_orders:
            logger.info("restored %d live pending orders from db", len(pending_orders))

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
            side = str(pos.side).upper()
            if side == "SHORT":
                # NOTE: 不对账 SHORT 持仓 — 当前 tracked_entries 仅追踪 LONG 成本 basis
                continue
            try:
                qty = abs(Decimal(str(pos.quantity)))
            except Exception:
                continue
            if qty <= 0:
                continue
            broker_qty[pos.symbol] = broker_qty.get(pos.symbol, Decimal("0")) + qty
        for symbol, (tracked_qty, tracked_cost) in snapshot.items():
            broker_have = broker_qty.get(symbol, Decimal("0"))
            if tracked_qty <= 0:
                continue
            drift_pct = abs(tracked_qty - broker_have) / tracked_qty
            if drift_pct < _POSITION_DRIFT_PCT_TOLERANCE and abs(tracked_qty - broker_have) < _POSITION_DRIFT_SHARE_TOLERANCE:
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

    @staticmethod
    def _broker_side_for_action(action: str) -> str:
        return "BUY" if action in {"BUY", "BUY_TO_COVER"} else "SELL"

    def _build_symbol_runtime(self, symbol: str, market: str, *, primary: bool = False) -> SymbolRuntime:
        if primary:
            engine = self.engine
        else:
            primary_params = self.engine.params
            engine = StrategyEngine(
                StrategyParams(
                    symbol=symbol,
                    market=market,
                    buy_low=primary_params.buy_low,
                    sell_high=primary_params.sell_high,
                    short_selling=primary_params.short_selling,
                    min_profit_amount=primary_params.min_profit_amount,
                    min_repricing_pct=primary_params.min_repricing_pct,
                    llm_action_cooldown_seconds=primary_params.llm_action_cooldown_seconds,
                    fee_rate_us=primary_params.fee_rate_us,
                    fee_rate_hk=primary_params.fee_rate_hk,
                )
            )
        return SymbolRuntime(
            symbol=symbol,
            market=market,
            engine=engine,
            recent_quotes=deque(maxlen=self._recent_quotes_cap),
        )

    def _sync_symbol_runtimes(self, db: Session) -> None:
        primary_symbol = self.engine.params.symbol
        symbol_markets: dict[str, str] = {}
        if primary_symbol:
            symbol_markets[primary_symbol] = self.engine.params.market
        try:
            watchlist_items = WatchlistService(db).list_items()
        except Exception:
            logger.warning("failed to load watchlist symbol runtimes; using primary symbol only", exc_info=True)
            watchlist_items = []
        for item in watchlist_items:
            symbol = getattr(item, "symbol", "")
            if not symbol:
                continue
            symbol_markets[symbol] = getattr(item, "market", "US") or "US"

        with self._state_lock:
            for symbol, market in symbol_markets.items():
                runtime = self._symbol_runtimes.get(symbol)
                if runtime is None:
                    runtime = self._build_symbol_runtime(
                        symbol,
                        market,
                        primary=symbol == primary_symbol,
                    )
                    self._symbol_runtimes[symbol] = runtime
                else:
                    runtime.market = market
                    runtime.engine.params.market = market
                    if symbol != primary_symbol:
                        runtime_params = runtime.engine.params
                        primary_params = self.engine.params
                        runtime_params.buy_low = primary_params.buy_low
                        runtime_params.sell_high = primary_params.sell_high
                        runtime_params.short_selling = primary_params.short_selling
                        runtime_params.min_profit_amount = primary_params.min_profit_amount
                        runtime_params.auto_resume_minutes = primary_params.auto_resume_minutes
                        runtime_params.fee_rate_us = primary_params.fee_rate_us
                        runtime_params.fee_rate_hk = primary_params.fee_rate_hk
                        runtime_params.min_repricing_pct = primary_params.min_repricing_pct
                        runtime_params.llm_action_cooldown_seconds = primary_params.llm_action_cooldown_seconds
                    if symbol == primary_symbol:
                        runtime.engine = self.engine
                if symbol != primary_symbol:
                    try:
                        self._state_svc.load_symbol_runtime(db, runtime.engine, symbol)
                    except Exception:
                        logger.warning("failed to load symbol runtime state for %s", symbol, exc_info=True)
            for symbol in list(self._symbol_runtimes):
                if symbol not in symbol_markets:
                    # Refuse to drop a runtime that is still tied to an
                    # in-flight pending order — the reconcile loop needs the
                    # engine to restore snapshots for that symbol. The
                    # pending will eventually clear (or the order will be
                    # cancelled by hand) and the next sync will then drop
                    # the runtime.
                    if self._trade_svc.pending_order_for(symbol) is not None:
                        logger.warning(
                            "skipping removal of symbol runtime for %s: "
                            "pending order still in flight",
                            symbol,
                        )
                        continue
                    del self._symbol_runtimes[symbol]

    def _remember_symbol_runtime_quote(self, quote: Quote, observed_at: datetime) -> None:
        with self._state_lock:
            runtime = self._symbol_runtimes.get(quote.symbol)
            if runtime is None:
                logger.warning("ignoring quote for unknown symbol %s", quote.symbol)
                return
            runtime.engine.record_price(quote.last_price)
            recent = runtime.recent_quotes
            recent.append(
                {
                    "symbol": quote.symbol,
                    "last_price": float(quote.last_price),
                    "bid": float(quote.bid),
                    "ask": float(quote.ask),
                    "timestamp": quote.timestamp,
                    "observed_at": observed_at,
                }
            )
            # Sliding-window prune: deque is bounded by maxlen, so we only
            # need to drop entries that are *stale by time*. Amortised O(1).
            cutoff = observed_at - timedelta(seconds=self._recent_quote_window_seconds)
            while recent and isinstance(recent[0].get("observed_at"), datetime) and recent[0]["observed_at"] < cutoff:
                recent.popleft()

    def _get_trading_session_mode(self) -> str:
        return self._trading_session_mode or "ANY"

    def _refresh_trading_session_mode(self) -> None:
        try:
            with self._db_session() as db:
                config = StrategyService(db).get_config()
            mode = getattr(config, "trading_session_mode", None)
        except Exception:
            logger.warning("failed to refresh trading_session_mode; keeping cache", exc_info=True)
            return
        self._trading_session_mode = mode if mode else "ANY"

    def _check_trading_session(
        self,
        action: str,
        symbol: str | None = None,
        market: str | None = None,
    ) -> dict[str, Any] | None:
        """Layer-A gate: block before cancel_pending when RTH_ONLY and outside RTH."""
        if action == "CANCEL_PENDING":
            return None
        if self._get_trading_session_mode() != "RTH_ONLY":
            return None
        target_market = market or self.engine.params.market
        if is_trading_hours(target_market):
            return None
        target_symbol = symbol or self.engine.params.symbol
        reason = f"non-RTH for {target_market}"
        self._record_order_skipped(
            target_symbol,
            action,
            reason,
            {"skip_category": "SESSION", "market": target_market},
        )
        if self._audit:
            self._audit.record(
                "TRADING_SESSION_BLOCKED",
                severity="INFO",
                request_summary={
                    "symbol": target_symbol,
                    "action": action,
                    "market": target_market,
                },
            )
        return {
            "executed": False,
            "status": "SKIPPED",
            "skip_category": "SESSION",
            "reason": reason,
            "order_id": None,
            "action": action,
        }

    def _skip_llm_action(
        self,
        action: str,
        reason: str,
        *,
        symbol: str | None = None,
        **payload: object,
    ) -> dict[str, Any]:
        self._record_order_skipped(symbol or self.engine.params.symbol, action, reason, payload)
        return {"executed": False, "status": "SKIPPED", "order_id": None, "action": action}

    def _precheck_llm_action(
        self,
        action: str,
        proposed_price: Any,
        pending: _PendingOrder | None,
        *,
        symbol: str | None = None,
        engine: StrategyEngine | None = None,
    ) -> dict[str, Any] | None:
        if symbol is not None and engine is not None:
            target_symbol = symbol
            target_engine = engine
        else:
            runtime = self._runtime_for_symbol(symbol)
            if runtime is None:
                return {"executed": False, "status": "UNKNOWN_SYMBOL", "order_id": None, "action": action}
            target_symbol, _, target_engine = runtime
        side = self._broker_side_for_action(action)
        params = target_engine.params
        if pending is not None and (params.min_repricing_pct or 0) > 0:
            normalized_price = self._coerce_positive_float(proposed_price)
            if normalized_price <= 0:
                return {"executed": False, "status": "NO_QUOTE", "order_id": None, "action": action}
            old_price = pending.price
            new_price = Decimal(str(normalized_price))
            if old_price <= 0:
                repricing_pct = Decimal("1")
            else:
                repricing_pct = abs(new_price - old_price) / old_price
            min_repricing = Decimal(str(params.min_repricing_pct or 0))
            if min_repricing > 0 and repricing_pct < min_repricing:
                return self._skip_llm_action(
                    action,
                    "replacement price movement is below minimum threshold",
                    symbol=target_symbol,
                    skip_category="REPRICING",
                    old_price=float(old_price),
                    new_price=float(new_price),
                    repricing_pct=float(repricing_pct),
                )
        cooldown = params.llm_action_cooldown_seconds or 0
        last_at = self._last_llm_action_at.get((target_symbol, side))
        if cooldown > 0 and last_at is not None:
            remaining = cooldown - (time.monotonic() - last_at)
            if remaining > 0:
                return self._skip_llm_action(
                    action,
                    "LLM action remains in cooldown",
                    symbol=target_symbol,
                    skip_category="COOLDOWN",
                    cooldown_remaining_seconds=remaining,
                )
        return None


_runner: AppRunner | None = None
_runner_lock = threading.Lock()


def get_runner() -> AppRunner:
    global _runner
    if _runner is None:
        with _runner_lock:
            if _runner is None:
                _runner = AppRunner()
    return _runner
