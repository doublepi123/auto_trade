from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace as dataclass_replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Deque, Generator, Optional, cast
from sqlalchemy.orm import Session

from app.api.deps import init_audit_logger
from app.api.ws import manager
from app.config import settings
from app.core.audit import AuditLogger
from app.core.broker import BrokerGateway, Position, Quote
from app.core.engine import EngineSnapshot, EngineState, StrategyEngine, StrategyParams, TriggerResult
from app.core.exit_policy import ExitPolicyConfig, ExitQuote, PositionExitContext, ReductionCause, ReductionDecision, evaluate_exit_policy
from app.core.fees import one_side_fee_rate
from app.core.market_calendar import is_closing_window, is_opening_warmup, is_trading_hours, trade_day_for
from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.core.notifiers.serverchan import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.database import SessionLocal
from app.models import OrderRecord, TrackedEntry, TradeEvent
from app.services.daily_pnl_service import DailyPnlService
from app.services.notification_log_service import get_notification_sink
from app.core.credential_crypto import CredentialIntegrityError
from app.services.credentials_service import CredentialsService, PlainCredentials
from app.services.runtime_state_service import (
    RuntimeStateService,
    hard_ceiling_float,
    hard_ceiling_int,
    hard_floor_int,
)
from app.services.llm_order_policy import evaluate_llm_order_policy
from app.services.strategy_service import StrategyService
from app.services.trade_event_service import record_trade_event
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    ORDER_EXECUTION_BLOCKED_PREFIX,
    ORDER_PERSISTENCE_UNCERTAIN_PREFIX,
    ORDER_STATUS_PERSISTENCE_UNCERTAIN_PREFIX,
    OrderPersistenceError,
    TradeExecutionService,
    _PendingOrder,
)
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
_ENTRY_ACTIONS = {"BUY", "SELL_SHORT"}
_POSITION_REDUCING_ACTIONS = {"SELL", "BUY_TO_COVER"}
_PENDING_TIMEOUT_PAUSE_RE = re.compile(r"pending order (?P<order_id>\S+) timed out after")
_ORDER_SUBMISSION_UNCERTAIN_PREFIX = "ORDER_SUBMISSION_UNCERTAIN:"
_POSITION_RECONCILIATION_UNCERTAIN_PREFIX = "POSITION_RECONCILIATION_UNCERTAIN:"
_REDUCTION_SETTLEMENT_UNCERTAIN_PREFIX = "REDUCTION_SETTLEMENT_UNCERTAIN:"
_ORDER_RECONCILIATION_UNCERTAIN_PREFIX = "ORDER_RECONCILIATION_UNCERTAIN:"
_PNL_RECONCILIATION_UNCERTAIN_PREFIX = "PNL_RECONCILIATION_UNCERTAIN:"
_POST_FILL_PNL_RECONCILIATION_PREFIX = (
    "post-fill PnL reconciliation in progress:"
)
_ORDER_SNAPSHOT_FETCH_ISSUE = (
    "broker today-order snapshot could not be represented or fetched"
)
_EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON = (
    f"{_ORDER_RECONCILIATION_UNCERTAIN_PREFIX} "
    "unresolved live orders require manual reconciliation; "
    f"live_orders=none; representation_issues={_ORDER_SNAPSHOT_FETCH_ISSUE}"
)
DISCONNECT_RETRY_EXHAUSTED_THRESHOLD = 3

_QUOTE_SPREAD_THRESHOLD_PCT = 0.05  # Reject quotes with >5% bid-ask spread
_QUOTE_LAST_BBO_DEVIATION_THRESHOLD_PCT = 0.005
_QUOTE_SOURCE_MAX_AGE_SECONDS = 30.0
_POST_FILL_SETTLEMENT_GRACE_SECONDS = 60.0
_UNKNOWN_SUBMISSION_RESUME_GRACE_SECONDS = 60.0
_UNKNOWN_SUBMISSION_SECOND_PROOF_SECONDS = 5.0
_ORDER_PROVENANCE_TIME_TOLERANCE_SECONDS = 600.0
_POSITION_DRIFT_PCT_TOLERANCE = Decimal("0.05")  # 5% position drift tolerance
_POSITION_DRIFT_SHARE_TOLERANCE = Decimal("1")  # 1 share absolute drift tolerance
_POSITION_DRIFT_SIGNATURE_QUANTUM = Decimal("0.000001")

_OPERATIONAL_PAUSE_PREFIXES = (
    _ORDER_SUBMISSION_UNCERTAIN_PREFIX,
    _POSITION_RECONCILIATION_UNCERTAIN_PREFIX,
    _REDUCTION_SETTLEMENT_UNCERTAIN_PREFIX,
    _ORDER_RECONCILIATION_UNCERTAIN_PREFIX,
    ORDER_EXECUTION_BLOCKED_PREFIX,
    ORDER_PERSISTENCE_UNCERTAIN_PREFIX,
    ORDER_STATUS_PERSISTENCE_UNCERTAIN_PREFIX,
    _PNL_RECONCILIATION_UNCERTAIN_PREFIX,
)


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
    allow_loss_exit: bool = False
    reduce_only: bool = False
    reduction_cause: str = ""
    reduction_intent: _ReductionIntent | None = None
    reduction_newly_latched: bool = False
    reduction_should_clear: bool = False
    early_return: bool = False


@dataclass(frozen=True)
class _ReductionIntent:
    action: str
    cause: str
    reason: str
    trigger_price: float
    started_at: datetime


@dataclass(frozen=True)
class _PostFillExpectation:
    side: str
    quantity: Decimal
    recorded_at: float
    cost: Decimal | None = None
    opened_at: datetime | None = None


class PrimarySwitchBlockedError(RuntimeError):
    """Raised when a primary-symbol change would abandon live execution state."""


class PrimarySwitchCheckError(RuntimeError):
    """Raised when the runner cannot prove that a primary-symbol change is safe."""


class CredentialSwitchBlockedError(RuntimeError):
    """Raised when a broker identity change could abandon live account state."""


class DurableFillReconciliationError(RuntimeError):
    """Raised when recent fills cannot be proved from the durable ledger."""


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
            on_reduction_fill=self._on_reduction_fill,
            audit=self._audit,
            margin_safety_factor=None,
            allow_position_addons=False,
            short_entries_enabled=settings.allow_short_entries,
            max_position_quantity=settings.hard_max_position_quantity,
            max_position_notional=settings.hard_max_position_notional,
            max_risk_per_trade=settings.hard_max_risk_per_trade,
            stop_loss_pct=settings.hard_stop_loss_pct,
            entry_cutoff_minutes_before_close=settings.hard_entry_cutoff_minutes_before_close,
            final_order_quote_check=self._validate_final_order_quote,
        )
        self._state_svc = RuntimeStateService()
        self._running = False
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._start_lock = threading.Lock()
        self._state_lock = threading.RLock()
        self._order_persistence_lock = threading.Lock()
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
        self._last_tracked_reconcile_at = 0.0
        self._tracked_reconcile_interval_seconds = 15.0
        self._last_order_sync_at = 0.0
        self._order_sync_interval_seconds = 15.0
        self._last_order_sync_succeeded = False
        self._fee_enrichment_next_retry_at: dict[str, float] = {}
        self._fee_enrichment_attempts: dict[str, int] = {}
        self._fee_enrichment_batch_size = 3
        self._unresolved_live_order_ids: list[str] = []
        self._unrepresentable_live_order_issues: list[str] = []
        self._recent_quote_window_seconds = 300.0
        self._recent_quotes_cap = 500
        self._recent_quotes: Deque[dict[str, Any]] = deque(maxlen=self._recent_quotes_cap)
        self._last_action_message = ""
        self._last_guarded_ledger_replay: tuple[object, ...] | None = None
        self._defer_incomplete_pnl_latch = False
        self._post_fill_pnl_pause_reason = ""
        self._tracked_avg_drift_warning_keys: dict[str, tuple[object, ...]] = {}
        self._last_llm_action_at: dict[tuple[str, str], float] = {}
        self._llm_order_execution_enabled = False
        self._reduction_intents: dict[str, _ReductionIntent] = {}
        # Per-symbol last fill timestamp. Previously a single float, which
        # caused a fill on symbol B to skip a position sync on symbol A
        # even though they are unrelated.
        self._last_fill_at: dict[str, float] = {}
        self._post_fill_expectations: dict[str, _PostFillExpectation] = {}
        self._unsettled_position_symbols: set[str] = set()
        self._unknown_submission_proof_reason = ""
        self._unknown_submission_proof_at = 0.0
        self._broker_identity_fingerprint = ""

    def _mark_fill_processed(self, symbol: str, action: str = "") -> None:
        tracked = self._trade_svc.tracked_position(symbol)
        expectation = _PostFillExpectation(
            side=tracked.side if tracked is not None else "",
            quantity=tracked.quantity if tracked is not None else Decimal("0"),
            recorded_at=time.monotonic(),
            cost=tracked.cost if tracked is not None else Decimal("0"),
            opened_at=tracked.opened_at if tracked is not None else None,
        )
        with self._trade_svc.submission_guard():
            with self._state_lock:
                fill_symbol = symbol or self.engine.params.symbol
                if fill_symbol:
                    self._last_fill_at[fill_symbol] = expectation.recorded_at
                    self._post_fill_expectations[fill_symbol] = expectation
            pause_reason = (
                f"{_POST_FILL_PNL_RECONCILIATION_PREFIX} "
                f"{fill_symbol or 'unknown symbol'}"
            )
            _, pause_created = self.risk.begin_entry_reconciliation(
                pause_reason,
                preserve_protective_exits=action in _POSITION_REDUCING_ACTIONS,
                auto_resumable=False,
            )
            if pause_created:
                with self._state_lock:
                    self._post_fill_pnl_pause_reason = pause_reason
                self._broadcast_status()
        # Persist immediately on fill so a crash before the 5s snapshot loop
        # does not lose the new engine state, tracked entry, or risk counters.
        # We do this in a best-effort fire-and-forget path because the
        # caller (trade_execution_service._finalize_pending_fill) is already
        # running under tight latency constraints.
        def _persist_async() -> None:
            reconciliation_complete = False
            reconciliation_trade_day: object = datetime.now(timezone.utc).date()
            try:
                reconciliation_trade_day = self._market_trade_day()
                with self._db_session() as db:
                    pnl_service = DailyPnlService(db)
                    pnl_service.refresh_execution_outcomes(symbol=fill_symbol or None)
                    ledger_result = pnl_service.calculate(
                        trade_day=self._market_trade_day(),
                        to_trade_day=self._market_trade_day_for,
                        fee_rate_us=self.engine.params.fee_rate_us,
                        fee_rate_hk=self.engine.params.fee_rate_hk,
                    )
                    if not ledger_result.is_complete:
                        logger.error(
                            "post-fill PnL replay is incomplete for %s; live risk state preserved",
                            fill_symbol,
                        )
                        reconciliation_trade_day = ledger_result.trade_day
                    else:
                        with self._state_lock:
                            net_pnl, net_losses = DailyPnlService.reconcile_risk_state(
                                self.risk.daily_pnl,
                                self.risk.consecutive_losses,
                                self.risk.daily_pnl_date,
                                ledger_result,
                            )
                            if ledger_result.trades:
                                self.risk.replace_daily_pnl(
                                    net_pnl,
                                    net_losses,
                                    ledger_result.trade_day,
                                )
                        self._state_svc.persist(db, self.engine, self.risk)
                        if fill_symbol:
                            runtime = self._symbol_runtimes.get(fill_symbol)
                            if runtime is not None and runtime.engine is not self.engine:
                                self._state_svc.persist_symbol(db, runtime.engine, fill_symbol)
                        reconciliation_complete = True
            except Exception:
                logger.exception("post-fill persist failed for %s", fill_symbol)
            finally:
                self._finish_post_fill_pnl_reconciliation(
                    is_complete=reconciliation_complete,
                    trade_day=reconciliation_trade_day,
                )

        try:
            threading.Thread(
                target=_persist_async,
                name="post-fill-persist",
                daemon=True,
            ).start()
        except Exception:
            logger.exception("post-fill persistence worker could not be started")
            fallback_trade_day: object = datetime.now(timezone.utc).date()
            try:
                fallback_trade_day = self._market_trade_day()
            except Exception:
                logger.exception("market trade day unavailable after worker start failure")
            self._finish_post_fill_pnl_reconciliation(
                is_complete=False,
                trade_day=fallback_trade_day,
            )

    def _finish_post_fill_pnl_reconciliation(
        self,
        *,
        is_complete: bool,
        trade_day: object,
    ) -> None:
        """Release the transient entry gate or convert it to a durable pause."""
        with self._trade_svc.submission_guard():
            remaining_reconciliations = self.risk.finish_entry_reconciliation()
            with self._state_lock:
                transient_reason = self._post_fill_pnl_pause_reason

            if not is_complete:
                self._latch_pnl_reconciliation_uncertain(trade_day)

            if remaining_reconciliations > 0:
                return

            with self._state_lock:
                if self._post_fill_pnl_pause_reason == transient_reason:
                    self._post_fill_pnl_pause_reason = ""
            if not is_complete or not transient_reason:
                return

            pause_reason, safety_generation = self.risk.pause_verification_snapshot()
            if pause_reason != transient_reason:
                return
            try:
                with self._db_session() as db:
                    resumed = self.risk.resume_if_pause_reason(
                        transient_reason,
                        expected_generation=safety_generation,
                        on_resumed=lambda: self._state_svc.persist(
                            db,
                            self.engine,
                            self.risk,
                        ),
                    )
            except Exception:
                logger.exception(
                    "failed to persist completed post-fill PnL reconciliation"
                )
                self._latch_pnl_reconciliation_uncertain(trade_day)
                return
            if resumed:
                self._broadcast_status()

    def _on_reduction_fill(
        self,
        symbol: str,
        action: str,
        fill_quantity: Decimal = Decimal("0"),
    ) -> None:
        if action not in _POSITION_REDUCING_ACTIONS:
            return
        with self._state_lock:
            intent = self._reduction_intents.get(symbol)

        runtime = self._runtime_for_symbol(symbol)
        reduction_engine = runtime[2] if runtime is not None else self.engine
        expected_side = "LONG" if action == "SELL" else "SHORT"
        tracked_after_fill = self._trade_svc.tracked_position(symbol)
        remaining_quantity = (
            tracked_after_fill.quantity if tracked_after_fill is not None else Decimal("0")
        )
        normalized_fill_quantity = max(Decimal("0"), Decimal(str(fill_quantity)))
        pre_fill_quantity = remaining_quantity + normalized_fill_quantity
        try:
            positions = self.broker.get_positions()
        except Exception:
            logger.exception("cannot confirm broker position after reduction fill for %s", symbol)
            self._latch_reduction_settlement_uncertain(
                symbol,
                reduction_engine,
                expected_side=expected_side,
                pre_fill_quantity=pre_fill_quantity,
            )
            return

        symbol_positions = [
            position
            for position in positions
            if position.symbol == symbol and Decimal(str(position.quantity)) > 0
        ]
        broker_sides = {str(position.side).upper() for position in symbol_positions}
        if symbol_positions:
            if broker_sides != {expected_side}:
                reason = (
                    f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                    f"unexpected broker position after {action} fill for {symbol}: "
                    f"{', '.join(sorted(broker_sides)) or 'UNKNOWN'}"
                )
                self.risk.pause(reason, auto_resumable=False)
                logger.critical(reason)
                if broker_sides in ({"LONG"}, {"SHORT"}):
                    actual_side = next(iter(broker_sides))
                    reduction_engine.sync_state(
                        has_long_position=actual_side == "LONG",
                        has_short_position=actual_side == "SHORT",
                    )
                self._persist_risk_pause_best_effort()
                return
            broker_quantity = sum(
                (Decimal(str(position.quantity)) for position in symbol_positions),
                Decimal("0"),
            )
            if broker_quantity > remaining_quantity:
                self._latch_reduction_settlement_uncertain(
                    symbol,
                    reduction_engine,
                    expected_side=expected_side,
                    pre_fill_quantity=pre_fill_quantity,
                )
                return
            reduction_engine.sync_state(
                has_long_position=expected_side == "LONG",
                has_short_position=expected_side == "SHORT",
            )
            return

        if self._trade_svc.tracked_position(symbol) is not None:
            self._trade_svc.clear_entry_price(symbol)
        if intent is None:
            reduction_engine.sync_state(False, False)
            return
        self._complete_reduction(
            symbol,
            cause=intent.cause,
            reason=intent.reason,
        )

    def _latch_reduction_settlement_uncertain(
        self,
        symbol: str,
        engine: StrategyEngine,
        *,
        expected_side: str,
        pre_fill_quantity: Decimal,
    ) -> None:
        engine.sync_state(
            has_long_position=expected_side == "LONG",
            has_short_position=expected_side == "SHORT",
        )
        reason = (
            f"{_REDUCTION_SETTLEMENT_UNCERTAIN_PREFIX} fill not reflected in "
            f"broker position for {symbol}; pre_fill_quantity={pre_fill_quantity}"
        )
        self.risk.pause(reason, auto_resumable=False)
        self._set_last_action_message(reason)
        self._persist_risk_pause_best_effort()
        self._broadcast_status()

    def _persist_risk_pause_best_effort(self) -> None:
        try:
            with self._db_session() as db:
                self._state_svc.persist(db, self.engine, self.risk)
        except Exception:
            logger.critical("failed to persist operational risk pause", exc_info=True)

    def _initialize_runner(self) -> None:
        with self._db_session() as db:
            config = self._state_svc.load(db, self.engine, self.risk)
            self._configure_live_safety(config)
            self._load_tracked_entries(db)
            self._sync_symbol_runtimes(db)
            self._restore_reduction(db)
            self._apply_credentials(self._load_credentials(), resubscribe=False)
        self._register_broker_disconnect_hook()
        self._refresh_trading_session_mode()

        self._defer_incomplete_pnl_latch = True
        try:
            self.sync_today_orders_from_broker(force=True)
            with self._db_session() as db:
                self._load_pending_orders(db)
                self._resume_pending_timeout_pause_if_filled(db)
            self._sync_risk_from_order_ledger()
            completed_reductions: list[tuple[str, _ReductionIntent]] = []
            with self._db_session() as db:
                self._pause_if_unresolved_live_order_exists(db)
                completed_reductions = self._reconcile_tracked_entries_with_broker(db)
            # Force an engine-vs-broker position sync BEFORE the quote
            # subscription is set up below. Without this, the engine state we
            # just loaded from DB can be stale for the first quote decisions.
            try:
                state_changed = self._sync_engine_state_with_positions(force=True)
                for symbol, intent in completed_reductions:
                    self._complete_reduction(
                        symbol,
                        cause=intent.cause,
                        reason=intent.reason,
                    )
                if (state_changed or self.risk.paused) and not completed_reductions:
                    with self._db_session() as db:
                        self._state_svc.persist(db, self.engine, self.risk)
            except Exception:
                logger.exception("initial engine state sync with broker positions failed")
        finally:
            self._defer_incomplete_pnl_latch = False
        self._sync_risk_from_order_ledger()
        self._resume_stale_post_fill_pause_after_startup()

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

    def _resume_stale_post_fill_pause_after_startup(self) -> bool:
        with self._state_lock:
            pause_reason = self.risk.pause_reason
            if (
                self.risk.entry_reconciliation_pending
                or not pause_reason.startswith(_POST_FILL_PNL_RECONCILIATION_PREFIX)
            ):
                return False

        def persist_resumed_state() -> None:
            with self._db_session() as db:
                self._state_svc.stage(db, self.engine, self.risk)
                record_trade_event(
                    db,
                    event_type="RISK_AUTO_RESUMED",
                    status="RUNNING",
                    message="stale post-fill PnL pause cleared after startup verification",
                    payload={
                        "source": "verified_stale_post_fill_reconciliation",
                        "pause_reason": pause_reason,
                    },
                )
                db.commit()

        try:
            resumed, error = self.resume_after_verification(
                on_resumed=persist_resumed_state,
            )
        except Exception:
            logger.exception("stale post-fill pause could not be cleared durably")
            return False
        if not resumed:
            logger.warning(
                "stale post-fill pause remains after startup verification: %s",
                error,
            )
            return False
        logger.info("cleared stale post-fill pause after complete startup verification")
        return True

    def _configure_live_safety(self, config: object) -> None:
        self._trade_svc.margin_safety_factor = getattr(config, "margin_safety_factor", None)
        self._trade_svc.allow_position_addons = bool(
            getattr(config, "allow_position_addons", False)
            and settings.hard_allow_position_addons
        )
        self._trade_svc.short_entries_enabled = settings.allow_short_entries
        self._trade_svc.max_position_quantity = hard_ceiling_int(
            getattr(config, "max_position_quantity", settings.hard_max_position_quantity),
            settings.hard_max_position_quantity,
        )
        self._trade_svc.max_position_notional = hard_ceiling_float(
            getattr(config, "max_position_notional", settings.hard_max_position_notional),
            settings.hard_max_position_notional,
        )
        self._trade_svc.max_risk_per_trade = hard_ceiling_float(
            getattr(config, "max_risk_per_trade", settings.hard_max_risk_per_trade),
            settings.hard_max_risk_per_trade,
        )
        self._trade_svc.stop_loss_pct = hard_ceiling_float(
            getattr(config, "stop_loss_pct", settings.hard_stop_loss_pct),
            settings.hard_stop_loss_pct,
        )
        self._trade_svc.entry_cutoff_minutes_before_close = hard_floor_int(
            getattr(
                config,
                "entry_cutoff_minutes_before_close",
                settings.hard_entry_cutoff_minutes_before_close,
            ),
            settings.hard_entry_cutoff_minutes_before_close,
        )
        self._llm_order_execution_enabled = bool(
            getattr(config, "llm_order_execution_enabled", False)
            and not settings.llm_shadow_mode
        )

    @staticmethod
    def _live_order_representation_issue(order: object, *, source: str) -> str | None:
        status = str(getattr(order, "status", "SUBMITTED") or "SUBMITTED").upper()
        try:
            executed_quantity = Decimal(
                str(getattr(order, "executed_quantity", 0) or 0)
            )
        except Exception:
            executed_quantity = Decimal("NaN")
        has_terminal_execution = status == "FILLED" or (
            status in {"CANCELLED", "REJECTED"}
            and executed_quantity.is_finite()
            and executed_quantity > 0
        )
        if status not in _LIVE_ORDER_STATUSES and not has_terminal_execution:
            return None
        order_id = str(getattr(order, "broker_order_id", "") or "").strip()
        symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        side = str(getattr(order, "side", "") or "").strip().upper()
        problems: list[str] = []
        if not order_id:
            problems.append("missing broker_order_id")
        if not symbol:
            problems.append("missing symbol")
        if side not in (_ENTRY_ACTIONS | _POSITION_REDUCING_ACTIONS):
            problems.append("invalid side")
        for field_name in ("quantity", "price"):
            try:
                value = Decimal(str(getattr(order, field_name, None)))
            except Exception:
                value = Decimal("NaN")
            if not value.is_finite() or value <= 0:
                problems.append(f"invalid {field_name}")
        if has_terminal_execution and (
            not executed_quantity.is_finite() or executed_quantity <= 0
        ):
            problems.append("invalid executed_quantity")
        if not problems:
            return None
        return (
            f"{source} id={order_id or '<missing>'} "
            f"symbol={symbol or '<missing>'}: {', '.join(problems)}"
        )

    @staticmethod
    def _has_terminal_execution(order: object) -> bool:
        status = str(getattr(order, "status", "SUBMITTED") or "SUBMITTED").upper()
        try:
            executed_quantity = Decimal(
                str(getattr(order, "executed_quantity", 0) or 0)
            )
        except Exception:
            return status == "FILLED"
        return status == "FILLED" or (
            status in {"CANCELLED", "REJECTED"}
            and executed_quantity.is_finite()
            and executed_quantity > 0
        )

    def _submission_event_matches_order(
        self,
        event: TradeEvent,
        order: object,
    ) -> bool:
        event_order_id = str(event.broker_order_id or "").strip()
        order_id = str(getattr(order, "broker_order_id", "") or "").strip()
        if not event_order_id or event_order_id != order_id:
            return False
        event_symbol = str(event.symbol or "").strip().upper()
        order_symbol = str(getattr(order, "symbol", "") or "").strip().upper()
        if not event_symbol or event_symbol != order_symbol:
            return False
        event_side = AppRunner._broker_side_for_action(str(event.side or ""))
        order_side = AppRunner._broker_side_for_action(
            str(getattr(order, "side", "") or "")
        )
        if not event_side or event_side != order_side:
            return False
        event_created_at = getattr(event, "created_at", None)
        order_created_at = getattr(order, "created_at", None)
        if event_created_at is None or order_created_at is None:
            return False
        if self._broker_identity_fingerprint:
            try:
                payload = json.loads(str(event.payload_json or "{}"))
            except (TypeError, ValueError):
                return False
            if (
                not isinstance(payload, dict)
                or payload.get("broker_identity_fingerprint")
                != self._broker_identity_fingerprint
            ):
                return False
        delta = abs(
            (
                AppRunner._as_utc(event_created_at)
                - AppRunner._as_utc(order_created_at)
            ).total_seconds()
        )
        return delta <= _ORDER_PROVENANCE_TIME_TOLERANCE_SECONDS

    def _live_order_inventory_from_db(
        self,
        db: Session,
    ) -> tuple[dict[str, list[str]], list[str]]:
        rows = (
            db.query(OrderRecord)
            .filter(OrderRecord.status.in_(_LIVE_ORDER_STATUSES))
            .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
            .all()
        )
        inventory: dict[str, list[str]] = {}
        issues: list[str] = []
        for row in rows:
            order_id = str(row.broker_order_id or "").strip()
            symbol = str(row.symbol or "").strip().upper()
            inventory.setdefault(symbol or "<missing-symbol>", []).append(
                order_id or f"<missing-id:row-{row.id}>"
            )
            issue = self._live_order_representation_issue(row, source="db live order")
            if issue is not None:
                issues.append(issue)
        return (
            {
                symbol: sorted(set(order_ids))
                for symbol, order_ids in sorted(inventory.items())
            },
            issues,
        )

    @staticmethod
    def _merge_live_order_inventories(
        *inventories: dict[str, list[str]],
    ) -> dict[str, list[str]]:
        merged: dict[str, set[str]] = {}
        for inventory in inventories:
            for symbol, order_ids in inventory.items():
                merged.setdefault(symbol, set()).update(order_ids)
        return {
            symbol: sorted(order_ids)
            for symbol, order_ids in sorted(merged.items())
        }

    def _latch_live_order_reconciliation(
        self,
        inventory: dict[str, list[str]],
        issues: list[str],
        *,
        require_any_live_order: bool = False,
    ) -> bool:
        primary_symbol = str(self.engine.params.symbol or "").strip().upper()
        non_primary = {
            symbol: order_ids
            for symbol, order_ids in inventory.items()
            if symbol != primary_symbol and order_ids
        }
        duplicate_symbols = {
            symbol: order_ids
            for symbol, order_ids in inventory.items()
            if len(set(order_ids)) > 1
        }
        unresolved_ids = sorted(
            {
                order_id
                for order_ids in inventory.values()
                for order_id in order_ids
            }
        )
        with self._state_lock:
            self._unresolved_live_order_ids = unresolved_ids
            self._unrepresentable_live_order_issues = sorted(set(issues))

        unsafe = bool(
            issues
            or non_primary
            or duplicate_symbols
            or (require_any_live_order and unresolved_ids)
        )
        if not unsafe:
            return False

        self.risk.revoke_protective_exits()

        inventory_text = "; ".join(
            f"{symbol}=[{', '.join(order_ids)}]"
            for symbol, order_ids in inventory.items()
        ) or "none"
        issue_text = "; ".join(sorted(set(issues))) or "none"
        reason = (
            f"{_ORDER_RECONCILIATION_UNCERTAIN_PREFIX} "
            "unresolved live orders require manual reconciliation; "
            f"live_orders={inventory_text}; representation_issues={issue_text}"
        )
        if self.risk.paused and self.risk.pause_reason == reason:
            return True

        logger.critical(reason)
        self.risk.pause(reason, auto_resumable=False)
        self._set_last_action_message(reason)
        self._persist_risk_pause_best_effort()
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record live-order reconciliation risk")
        self._broadcast_status()
        return True

    def _pause_if_unresolved_live_order_exists(self, db: Session) -> bool:
        inventory, issues = self._live_order_inventory_from_db(db)
        inventory = self._merge_live_order_inventories(
            inventory,
            self._trade_svc.pending_order_inventory(),
        )
        return self._latch_live_order_reconciliation(
            inventory,
            issues,
            require_any_live_order=True,
        )

    def pause_for_manual_control(self, reason: str) -> bool:
        """Pause manually without replacing a latched operational diagnosis."""
        with self._trade_svc.submission_guard():
            with self._state_lock:
                self.risk.revoke_protective_exits()
                if self.risk.paused and (
                    self.risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES)
                    or self._unresolved_live_order_ids
                    or self._unrepresentable_live_order_issues
                ):
                    return False
                self.risk.pause(reason)
                return True

    def activate_kill_switch(self, reason: str) -> None:
        with self._trade_svc.submission_guard():
            self.pause_for_manual_control(reason)
            self.risk.enable_kill_switch(reason)

    def prepare_stop(self, reason: str) -> None:
        with self._trade_svc.submission_guard():
            self.pause_for_manual_control(reason)
            with self._state_lock:
                self._running = False

    def resume_after_verification(
        self,
        *,
        on_resumed: Callable[[], None] | None = None,
    ) -> tuple[bool, str]:
        with self._trade_svc.submission_guard():
            self.risk.revoke_protective_exits()
            pause_reason, safety_generation = self.risk.pause_verification_snapshot()
            safe, error = self.verify_operational_resume()
            if not safe:
                self._broadcast_status()
                return False, error
            if not self.risk.resume_if_pause_reason(
                pause_reason,
                expected_generation=safety_generation,
                on_resumed=on_resumed,
            ):
                self._broadcast_status()
                return False, "operational pause changed during verification"
            self._broadcast_status()
            return True, ""

    def permit_protective_exits_after_verification(self) -> tuple[bool, str]:
        """Arm reduce-only execution while retaining the operational pause."""
        with self._trade_svc.submission_guard():
            self.risk.revoke_protective_exits()
            healthy, health_error = self._protective_exit_runtime_health()
            if not healthy:
                self._broadcast_status()
                return False, health_error
            pause_reason, safety_generation = self.risk.pause_verification_snapshot()
            safe, error = self.verify_operational_resume(
                require_complete_pnl=False,
            )
            if not safe:
                self._broadcast_status()
                return False, error
            healthy, health_error = self._protective_exit_runtime_health()
            if not healthy:
                self._broadcast_status()
                return False, health_error
            if not self.risk.permit_protective_exits(
                expected_pause_reason=pause_reason,
                expected_generation=safety_generation,
            ):
                self._broadcast_status()
                return False, "protective exits require an unchanged operational pause"
            self._broadcast_status()
            return True, ""

    def _protective_exit_runtime_health(self) -> tuple[bool, str]:
        """Require a live runner and a recent trusted quote before arming exits."""
        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            running = self._running and thread_alive
            quotes_subscribed = self._quotes_subscribed
            last_quote_at = self._last_quote_at
        if not running:
            return False, "protective exits require the runner thread to be running"
        if not quotes_subscribed:
            return False, "protective exits require an active quote subscription"
        if last_quote_at <= 0:
            return False, "protective exits require a trusted live quote"
        max_quote_age = max(
            _QUOTE_SOURCE_MAX_AGE_SECONDS,
            self._active_quote_refresh_interval_seconds * 2,
        )
        quote_age = time.monotonic() - last_quote_at
        if quote_age > max_quote_age:
            return (
                False,
                "protective exits require a healthy quote loop; "
                f"last trusted quote is {quote_age:.1f}s old",
            )
        return True, ""

    def revoke_protective_exits(self) -> None:
        with self._trade_svc.submission_guard():
            self.risk.revoke_protective_exits()
            self._broadcast_status()

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
                if self._thread.is_alive():
                    logger.critical(
                        "runner start blocked: previous runner thread is still alive"
                    )
                    return False
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
        tracked_entries = self._trade_svc.snapshot_tracked_entries()
        max_quantity = float(self._trade_svc.max_position_quantity or 0)
        max_notional = float(self._trade_svc.max_position_notional or 0)
        max_risk = float(self._trade_svc.max_risk_per_trade or 0)
        stop_loss_pct = float(self._trade_svc.stop_loss_pct or 0)

        def age_since(value: float) -> float | None:
            if value <= 0:
                return None
            return max(0.0, now - value)

        def exposure(symbol: str, last_price: float) -> dict[str, object]:
            quantity, cost = tracked_entries.get(
                symbol,
                (Decimal("0"), Decimal("0")),
            )
            quantity_value = float(quantity)
            avg_price = float(cost / quantity) if quantity > 0 else 0.0
            reference_price = last_price if last_price > 0 else avg_price
            notional = quantity_value * reference_price
            risk_at_stop = float(cost) * stop_loss_pct / 100 if cost > 0 else 0.0
            breaches: list[str] = []
            if max_quantity > 0 and quantity_value > max_quantity:
                breaches.append("MAX_POSITION_QUANTITY")
            if max_notional > 0 and notional > max_notional:
                breaches.append("MAX_POSITION_NOTIONAL")
            if max_risk > 0 and risk_at_stop > max_risk:
                breaches.append("MAX_RISK_PER_TRADE")
            return {
                "position_quantity": quantity_value,
                "position_avg_price": avg_price,
                "position_notional": notional,
                "position_risk_at_stop": risk_at_stop,
                "position_limit_breaches": breaches,
            }

        with self._state_lock:
            thread_alive = self._thread is not None and self._thread.is_alive()
            pending_order_symbols = sorted(getattr(self._trade_svc, "_pending_orders", {}).keys())
            pending_order_ids = sorted(
                set(self._trade_svc.pending_order_ids())
                | set(self._unresolved_live_order_ids)
            )
            unrepresentable_live_order_issues = list(
                self._unrepresentable_live_order_issues
            )
            primary_symbol = self.engine.params.symbol
            symbol_runtimes = []
            for symbol, runtime in sorted(self._symbol_runtimes.items()):
                last_price = float(runtime.engine.last_price)
                symbol_runtimes.append({
                    "symbol": runtime.symbol,
                    "market": runtime.market,
                    "is_primary": symbol == primary_symbol,
                    "trading_enabled": symbol == primary_symbol,
                    "engine_state": runtime.engine.state.value,
                    "last_price": last_price,
                    "last_trigger_price": float(runtime.engine.last_trigger_price),
                    "recent_quote_count": len(runtime.recent_quotes),
                    "has_pending_order": self._trade_svc.pending_order_for(symbol) is not None,
                    "quote_quality": self._quote_quality_for_runtime(runtime),
                    **exposure(symbol, last_price),
                })
            if primary_symbol and primary_symbol not in self._symbol_runtimes:
                last_price = float(self.engine.last_price)
                symbol_runtimes.insert(
                    0,
                    {
                        "symbol": primary_symbol,
                        "market": self.engine.params.market,
                        "is_primary": True,
                        "trading_enabled": True,
                        "engine_state": self.engine.state.value,
                        "last_price": last_price,
                        "last_trigger_price": float(self.engine.last_trigger_price),
                        "recent_quote_count": len(self._recent_quotes),
                        "has_pending_order": self._trade_svc.pending_order_for(primary_symbol) is not None,
                        "quote_quality": self._quote_quality_for_primary(),
                        **exposure(primary_symbol, last_price),
                    },
                )
            return {
                "runner_running": self._running and thread_alive,
                "thread_alive": thread_alive,
                "quotes_subscribed": self._quotes_subscribed,
                "trigger_in_flight": self._trigger_in_flight,
                "pending_order_symbols": pending_order_symbols,
                "pending_order_ids": pending_order_ids,
                "unrepresentable_live_order_issues": unrepresentable_live_order_issues,
                "order_sync_succeeded": self._last_order_sync_succeeded,
                "execution_state": self.execution_state()[0],
                "reduction_reason": self.execution_state()[1],
                "live_safety": {
                    "short_entries_enabled": bool(
                        self._trade_svc.short_entries_enabled and self.engine.params.short_selling
                    ),
                    "allow_position_addons": bool(
                        self._trade_svc.allow_position_addons
                        and self.engine.params.allow_position_addons
                    ),
                    "max_position_quantity": int(self._trade_svc.max_position_quantity or 0),
                    "max_position_notional": float(self._trade_svc.max_position_notional or 0),
                    "max_risk_per_trade": float(self._trade_svc.max_risk_per_trade or 0),
                    "stop_loss_pct": float(self._trade_svc.stop_loss_pct or 0),
                    "max_holding_minutes": int(self.engine.params.max_holding_minutes),
                    "entry_cutoff_minutes_before_close": int(
                        self._trade_svc.entry_cutoff_minutes_before_close
                    ),
                    "flatten_minutes_before_close": int(
                        self.engine.params.flatten_minutes_before_close
                    ),
                    "llm_shadow_mode": bool(settings.llm_shadow_mode),
                    "llm_order_execution_enabled": bool(self._llm_order_execution_enabled),
                },
                "quote_stream": {
                    "last_push_age_seconds": age_since(self._last_push_quote_at),
                    "last_quote_age_seconds": age_since(self._last_quote_at),
                    "recent_quote_count": len(self._recent_quotes),
                },
                "risk": {
                    "paused": self.risk.paused,
                    "kill_switch": self.risk.kill_switch,
                    "pause_reason": self.risk.pause_reason,
                    "protective_exit_permitted": self.risk.protective_exit_permitted,
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
        with self._start_lock:
            defer_broker_close = False
            with self._state_lock:
                self._running = False
                self._quotes_subscribed = False
                if self._trigger_in_flight:
                    self._defer_broker_close = True
                    defer_broker_close = True
            # A broker SDK call can outlive the polite shutdown timeout. The
            # thread remains the authoritative lifecycle guard: start() and
            # credential switching refuse to proceed until it actually exits.
            trigger_thread = self._thread
            if trigger_thread is not None and trigger_thread.is_alive():
                trigger_thread.join(timeout=10)
                if trigger_thread.is_alive():
                    logger.warning(
                        "runner thread did not exit within 10s of stop(); "
                        "forcing broker.close() while restart stays blocked"
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

    def reload_credentials(self, *, broker_identity_change: bool = True) -> None:
        credentials = self._load_credentials()
        if not broker_identity_change:
            effective_credentials = (
                credentials
                if credentials.sct_key
                else dataclass_replace(credentials, sct_key=settings.sct_key)
            )
            new_notifier = MultiChannelNotifier.from_credential_config(
                effective_credentials,
                sink=get_notification_sink().record,
            )
            with self._state_lock:
                old_notifier = self.notifier
                self.notifier = new_notifier
            close_notifier = getattr(old_notifier, "close", None)
            if callable(close_notifier):
                try:
                    close_notifier()
                except Exception as exc:
                    logger.warning("error closing previous notifier: %s", exc)
            return

        with self._start_lock:
            self.assert_credential_switch_safe()
            self._apply_credentials(
                credentials,
                resubscribe=False,
                validate_switch=True,
            )

    def assert_credential_switch_safe(self) -> None:
        with self._state_lock:
            if self._running:
                raise CredentialSwitchBlockedError(
                    "runner must be stopped before broker credentials can change"
                )
            if self._thread is not None and self._thread.is_alive():
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while the previous runner "
                    "thread is still alive"
                )
            if self._trigger_in_flight:
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while an order trigger is in flight"
                )
            if self._trade_svc.has_pending_order:
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while orders are pending"
                )
            if self._reduction_intents:
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while reduction is active"
                )
            if self._trade_svc.snapshot_tracked_entries():
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while positions are tracked"
                )
            if self.engine.state != EngineState.FLAT:
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change while the engine is not flat"
                )
            if self.risk.paused and self.risk.pause_reason.startswith(
                _OPERATIONAL_PAUSE_PREFIXES
            ):
                raise CredentialSwitchBlockedError(
                    "broker credentials cannot change during unresolved reconciliation"
                )

        try:
            positions = self.broker.get_positions()
            orders = self.broker.get_today_orders()
        except Exception as exc:
            raise CredentialSwitchBlockedError(
                "cannot verify the current broker account before credential change"
            ) from exc
        exposed = sorted(
            {
                position.symbol
                for position in positions
                if Decimal(str(position.quantity)) > 0
            }
        )
        if exposed:
            raise CredentialSwitchBlockedError(
                "current broker account still has positions: " + ", ".join(exposed)
            )
        live_order_ids = sorted(
            {
                str(order.broker_order_id)
                for order in orders
                if str(order.status).upper() not in _TERMINAL_ORDER_STATUSES
            }
        )
        if live_order_ids:
            raise CredentialSwitchBlockedError(
                "current broker account still has live orders: "
                + ", ".join(live_order_ids)
            )

    def assert_primary_switch_safe(self, new_symbol: str, new_market: str) -> None:
        normalized_symbol = (new_symbol or "").strip().upper()
        normalized_market = (new_market or "").strip().upper()
        with self._state_lock:
            current_symbol = (self.engine.params.symbol or "").strip().upper()
            current_market = (self.engine.params.market or "").strip().upper()
            if normalized_symbol == current_symbol and normalized_market == current_market:
                return
            if self._trigger_in_flight:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while an order trigger is in flight"
                )
            pending_symbols = sorted(getattr(self._trade_svc, "_pending_orders", {}))
            if pending_symbols:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while orders are pending: "
                    + ", ".join(pending_symbols)
                )
            if self._reduction_intents:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while deterministic reduction is active"
                )
            if self._post_fill_expectations:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while fill settlement is pending"
                )
            if self.risk.paused and self.risk.pause_reason.startswith(
                _OPERATIONAL_PAUSE_PREFIXES
            ):
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change during unresolved reconciliation"
                )
            tracked_symbols = sorted(self._trade_svc.snapshot_tracked_entries())
            if tracked_symbols:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while positions are tracked: "
                    + ", ".join(tracked_symbols)
                )
            if self.engine.state != EngineState.FLAT:
                raise PrimarySwitchBlockedError(
                    f"primary strategy cannot change while engine state is {self.engine.state.value}"
                )
            if current_symbol and not self._running:
                raise PrimarySwitchCheckError(
                    "primary strategy can only change while the runner is active and flat"
                )
            try:
                broker_positions = self.broker.get_positions()
            except Exception as exc:
                raise PrimarySwitchCheckError(
                    "cannot verify broker positions before primary strategy change"
                ) from exc
            exposed_symbols = sorted(
                {
                    str(position.symbol)
                    for position in broker_positions
                    if Decimal(str(position.quantity)) > 0
                }
            )
            if exposed_symbols:
                raise PrimarySwitchBlockedError(
                    "primary strategy cannot change while broker positions exist: "
                    + ", ".join(exposed_symbols)
                )

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
                short_selling=bool(config.short_selling and settings.allow_short_entries),
                min_profit_amount=config.min_profit_amount,
                auto_resume_minutes=config.auto_resume_minutes,
                fee_rate_us=config.fee_rate_us,
                fee_rate_hk=config.fee_rate_hk,
                min_repricing_pct=config.min_repricing_pct,
                llm_action_cooldown_seconds=config.llm_action_cooldown_seconds,
                allow_position_addons=bool(
                    getattr(config, "allow_position_addons", False)
                    and settings.hard_allow_position_addons
                ),
                stop_loss_pct=hard_ceiling_float(
                    getattr(config, "stop_loss_pct", settings.hard_stop_loss_pct),
                    settings.hard_stop_loss_pct,
                ),
                max_holding_minutes=hard_ceiling_int(
                    getattr(
                        config,
                        "max_holding_minutes",
                        settings.hard_max_holding_minutes,
                    ),
                    settings.hard_max_holding_minutes,
                ),
                entry_cutoff_minutes_before_close=hard_floor_int(
                    getattr(
                        config,
                        "entry_cutoff_minutes_before_close",
                        settings.hard_entry_cutoff_minutes_before_close,
                    ),
                    settings.hard_entry_cutoff_minutes_before_close,
                ),
                flatten_minutes_before_close=hard_floor_int(
                    getattr(
                        config,
                        "flatten_minutes_before_close",
                        settings.hard_flatten_minutes_before_close,
                    ),
                    settings.hard_flatten_minutes_before_close,
                ),
            )
            new_risk_config = RiskConfig(
                max_daily_loss=config.max_daily_loss,
                max_consecutive_losses=config.max_consecutive_losses,
            )
            mode = getattr(config, "trading_session_mode", None)
            new_session_mode = mode if mode else "ANY"

            with self._state_lock:
                previous_symbol = self.engine.params.symbol
                previous_market = self.engine.params.market
            primary_changed = (
                new_params.symbol != previous_symbol or new_params.market != previous_market
            )
            candidate_engine: StrategyEngine | None = None
            if primary_changed:
                self.assert_primary_switch_safe(new_params.symbol, new_params.market)
                candidate_engine = StrategyEngine(new_params)
                self._state_svc.load_symbol_runtime(
                    db,
                    candidate_engine,
                    new_params.symbol,
                )
                if self._running:
                    candidate_engine.sync_state(False, False)

            need_resubscribe = False
            resubscribe_symbols: list[str] = []
            with self._state_lock:
                if primary_changed:
                    # Re-check under the same lock used by quote evaluation so
                    # no trigger can appear between the broker proof and swap.
                    self.assert_primary_switch_safe(new_params.symbol, new_params.market)
                previous_quote_symbols = set(self._desired_quote_symbols_locked())
                if candidate_engine is not None:
                    self.engine = candidate_engine
                else:
                    self.engine.params = new_params
                self.risk.config = new_risk_config
                self._trading_session_mode = new_session_mode
                self._configure_live_safety(config)
                self._sync_symbol_runtimes(db)
                resubscribe_symbols = self._desired_quote_symbols_locked()
                quote_symbols_changed = previous_quote_symbols != set(resubscribe_symbols)
                if self._running and quote_symbols_changed:
                    self._reset_quote_tracking(clear_history=True)
                    if self._quotes_subscribed:
                        need_resubscribe = True
                        self._quotes_subscribed = False

            if need_resubscribe:
                try:
                    self.broker.unsubscribe_quotes()
                except Exception:
                    logger.warning("failed to unsubscribe old symbols during strategy reload")
                if resubscribe_symbols:
                    try:
                        self._subscribe_quote_symbols(self.broker, resubscribe_symbols)
                        with self._state_lock:
                            self._quotes_subscribed = True
                            self._last_push_quote_at = time.monotonic()
                        logger.info(
                            "re-subscribed to quote streams after strategy reload: %s",
                            ", ".join(resubscribe_symbols),
                        )
                    except Exception as exc:
                        logger.error("quote subscription failed after strategy reload: %s", exc)
                        reason = f"quote subscription failed after strategy reload: {exc}"
                        self.risk.pause(reason, auto_resumable=False)
                        raise RuntimeError(reason) from exc
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
            if not is_primary_symbol:
                decision.early_return = True
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
            quote_quality = self._evaluate_quote_quality({
                "last_price": quote.last_price,
                "bid": quote.bid,
                "ask": quote.ask,
                "timestamp": quote.timestamp,
            })
            if (
                not quote_quality["price_positive"]
                or not quote_quality["spread_reasonable"]
                or not quote_quality["last_bbo_consistent"]
                or not quote_quality["source_timestamp_fresh"]
            ):
                self._last_action_message = (
                    f"{quote.symbol} quote rejected by live quality gate"
                )
                decision.early_return = True
                return decision
            risk_result = self.risk.check()
            if self._trade_svc.pending_order_for(quote.symbol) is not None:
                self._trigger_in_flight = True
                decision.processing_started = True
            else:
                reduction_intent, newly_latched, should_clear = (
                    self._reduction_intent_for_quote_locked(quote, active_engine, active_market)
                )
                decision.reduction_should_clear = should_clear
                if reduction_intent is not None:
                    decision.reduction_intent = reduction_intent
                    decision.reduction_newly_latched = newly_latched
                    if self._get_trading_session_mode() == "RTH_ONLY" and not is_trading_hours(
                        active_market
                    ):
                        active_engine.record_price(quote.last_price)
                        return decision
                    decision.engine_snapshot = active_engine.snapshot()
                    transition_status = active_engine.transition_for_action(reduction_intent.action)
                    if transition_status == "OK":
                        decision.result = TriggerResult(
                            triggered=True,
                            action=reduction_intent.action,
                            description=reduction_intent.reason,
                        )
                        decision.allow_loss_exit = True
                        decision.reduce_only = True
                        decision.reduction_cause = reduction_intent.cause
                    else:
                        logger.error(
                            "reduction transition rejected for %s: action=%s state=%s status=%s",
                            quote.symbol,
                            reduction_intent.action,
                            active_engine.state.value,
                            transition_status,
                        )
                elif (
                    active_engine.state == EngineState.FLAT
                    and active_engine.long_entry_rearm_required
                    and self._get_trading_session_mode() == "RTH_ONLY"
                    and not is_trading_hours(active_market)
                ):
                    # Extended-hours quotes may update observability, but they
                    # must not re-arm the next regular-session entry.
                    active_engine.record_price(quote.last_price)
                elif not risk_result.approved and not self._should_evaluate_reducing_trigger(
                    active_engine,
                    float(quote.last_price),
                ):
                    if (
                        active_engine.state == EngineState.FLAT
                        and active_engine.long_entry_rearm_required
                    ):
                        # Risk limits block orders, not observation of a valid
                        # in-session reclaim needed for a future fresh breach.
                        active_engine.update_price(quote.last_price)
                    else:
                        active_engine.record_price(quote.last_price)
                else:
                    decision.engine_snapshot = active_engine.snapshot()
                    decision.result = active_engine.update_price(quote.last_price)
                if decision.result is not None and decision.result.triggered:
                    if (
                        not risk_result.approved
                        and not self._risk_rejection_allows_action(decision.result.action)
                    ):
                        if decision.engine_snapshot is not None:
                            active_engine.restore(decision.engine_snapshot)
                        active_engine.record_price(quote.last_price)
                        decision.result = None
                        return decision
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
        if not risk_result.approved and not self._risk_rejection_allows_action(result.action):
            logger.warning("risk rejected: %s", risk_result.reason)
            self._set_last_action_message(f"{result.action} rejected by risk: {risk_result.reason}")
            self._record_risk_event(risk_result.reason)
            self.notifier.notify_risk_event("REJECTED", risk_result.reason)
            if engine_snapshot is not None:
                restore_engine_snapshot(engine_snapshot)
            self._broadcast_status()
            return

        try:
            ledger_context = self._execution_ledger_context(
                decision,
                quote,
                result.description,
            )
            execution_quote = quote
            if decision.reduce_only:
                executable_price = (
                    float(quote.bid)
                    if result.action == "SELL" and float(quote.bid) > 0
                    else float(quote.ask)
                    if result.action == "BUY_TO_COVER" and float(quote.ask) > 0
                    else float(quote.last_price)
                )
                execution_quote = Quote(
                    symbol=quote.symbol,
                    last_price=executable_price,
                    bid=quote.bid,
                    ask=quote.ask,
                    timestamp=quote.timestamp,
                )
            order_status = self._trade_svc.execute(
                action=result.action,
                symbol=decision.trigger_symbol or quote.symbol,
                quote=execution_quote,
                broker=self.broker,
                risk=self.risk,
                notifier=self.notifier,
                cash_currency=self._cash_currency_for_market(decision.trigger_market),
                market=decision.trigger_market,
                trading_session_mode=self._get_trading_session_mode(),
                min_profit_amount=trigger_engine.params.min_profit_amount,
                allow_loss_exit=decision.allow_loss_exit,
                fee_rate=self._live_fee_rate_for_market(decision.trigger_market),
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=self.notifier.notify_risk_event,
                reduce_only=decision.reduce_only,
                execution_context=ledger_context,
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
                if decision.reduce_only and order_status.status == "FILLED":
                    target_symbol = decision.trigger_symbol or quote.symbol
                    self._on_reduction_fill(
                        target_symbol,
                        result.action,
                        Decimal(str(order_status.executed_quantity or 0)),
                    )
                    with self._state_lock:
                        reduction_still_active = target_symbol in self._reduction_intents
                    if reduction_still_active and engine_snapshot is not None:
                        restore_engine_snapshot(engine_snapshot)
                        self._set_last_action_message(
                            f"{result.action} FILLED but broker position remains; reduction stays active"
                        )
            self._broadcast_status()
        except Exception as exc:
            if engine_snapshot is not None:
                restore_engine_snapshot(engine_snapshot)
            uncertain_symbol = decision.trigger_symbol or quote.symbol
            reason = (
                f"{_ORDER_SUBMISSION_UNCERTAIN_PREFIX} symbol={uncertain_symbol} "
                f"action={result.action} order execution failed: {exc}"
            )
            self._set_last_action_message(reason)
            self.risk.pause(reason, auto_resumable=False)
            try:
                with self._db_session() as db:
                    self._state_svc.persist(db, self.engine, self.risk)
            except Exception:
                logger.critical("failed to persist uncertain-order pause", exc_info=True)
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

    def _execution_ledger_context(
        self,
        decision: _QuoteTriggerDecision,
        quote: Quote,
        reason: str,
    ) -> dict[str, object]:
        now = datetime.now(timezone.utc)
        bid = float(quote.bid)
        ask = float(quote.ask)
        midpoint = (bid + ask) / 2 if bid > 0 and ask > 0 else 0.0
        spread = ask - bid if midpoint > 0 else 0.0
        params = (
            decision.trigger_engine.params
            if decision.trigger_engine is not None
            else self.engine.params
        )
        snapshot = {
            "strategy": asdict(params),
            "trading_session_mode": self._get_trading_session_mode(),
            "reduce_only": decision.reduce_only,
            "allow_short_entries": settings.allow_short_entries,
            "risk": {
                "max_daily_loss": self.risk.config.max_daily_loss,
                "max_consecutive_losses": self.risk.config.max_consecutive_losses,
            },
            "hard_limits": {
                "max_position_quantity": settings.hard_max_position_quantity,
                "max_position_notional": settings.hard_max_position_notional,
                "max_risk_per_trade": settings.hard_max_risk_per_trade,
                "stop_loss_pct": settings.hard_stop_loss_pct,
            },
        }
        snapshot_json = json.dumps(
            snapshot,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )
        is_exit = bool(
            decision.result is not None
            and decision.result.action in _POSITION_REDUCING_ACTIONS
        )
        return {
            "decision_at": now,
            "decision_bid": bid if bid > 0 else None,
            "decision_ask": ask if ask > 0 else None,
            "decision_spread": spread if midpoint > 0 else None,
            "decision_spread_bps": spread / midpoint * 10_000 if midpoint > 0 else None,
            "quote_age_ms": self._quote_age_ms(quote.timestamp, now),
            "config_version": hashlib.sha256(snapshot_json.encode("utf-8")).hexdigest(),
            "config_snapshot": snapshot_json,
            "exit_cause": (
                decision.reduction_cause or "TARGET" if is_exit else ""
            ),
            "exit_reason": reason if is_exit else "",
        }

    @staticmethod
    def _quote_age_ms(value: object, now: datetime | None = None) -> float | None:
        raw = str(value or "").strip()
        if not raw:
            return None
        try:
            if raw.replace(".", "", 1).isdigit():
                numeric = float(raw)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                source_time = datetime.fromtimestamp(numeric, tz=timezone.utc)
            else:
                source_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                source_time = (
                    source_time.replace(tzinfo=timezone.utc)
                    if source_time.tzinfo is None
                    else source_time.astimezone(timezone.utc)
                )
        except (ValueError, OverflowError, OSError):
            return None
        return max(
            0.0,
            ((now or datetime.now(timezone.utc)) - source_time).total_seconds() * 1000,
        )

    def _on_quote(self, quote: Quote, *, is_push: bool = True) -> None:
        processing_started = False
        try:
            quote_quality = self._evaluate_quote_quality(
                {
                    "last_price": quote.last_price,
                    "bid": quote.bid,
                    "ask": quote.ask,
                    "timestamp": quote.timestamp,
                }
            )
            if (
                quote.symbol == self.engine.params.symbol
                and quote_quality["price_positive"]
                and quote_quality["spread_reasonable"]
                and quote_quality["last_bbo_consistent"]
                and quote_quality["source_timestamp_fresh"]
            ):
                self._pause_if_unrealized_loss_limit_reached(
                    quote.symbol,
                    float(quote.last_price),
                )
            decision = self._evaluate_quote_trigger(quote, is_push=is_push)
            processing_started = decision.processing_started

            if decision.reduction_should_clear:
                if not self._clear_reduction(quote.symbol, reason="position is flat"):
                    return
            if decision.reduction_newly_latched and decision.reduction_intent is not None:
                if not self._persist_reduction(decision.reduction_intent, quote.symbol):
                    if decision.engine_snapshot is not None and decision.trigger_engine is not None:
                        decision.trigger_engine.restore(decision.engine_snapshot)
                    reason = (
                        f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                        f"failed to persist reduction intent for {quote.symbol}"
                    )
                    self.risk.pause(reason, auto_resumable=False)
                    self._set_last_action_message(reason)
                    self._broadcast_status()
                    return

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

        requested_symbol = str(decision.get("symbol") or self.engine.params.symbol).upper()
        if requested_symbol != self.engine.params.symbol:
            return {
                "executed": False,
                "status": "WATCHLIST_READ_ONLY",
                "order_id": None,
                "action": action,
                "reason": "only the primary strategy symbol may submit live orders",
            }

        runtime = self._runtime_for_symbol(requested_symbol)
        if runtime is None:
            return {"executed": False, "status": "UNKNOWN_SYMBOL", "order_id": None, "action": ""}
        target_symbol, target_market, target_engine = runtime

        reference_quote = (
            None if action == "CANCEL_PENDING" else self._trusted_quote_for_llm_policy(target_symbol)
        )
        policy = evaluate_llm_order_policy(
            decision,
            min_confidence=settings.llm_min_confidence,
            max_price_deviation_pct=settings.llm_max_order_price_deviation_pct,
            execution_enabled=self._llm_order_execution_enabled,
            shadow_mode=settings.llm_shadow_mode,
            reference_bid=float(reference_quote.bid) if reference_quote is not None else None,
            reference_ask=float(reference_quote.ask) if reference_quote is not None else None,
            short_entries_enabled=settings.allow_short_entries,
        )
        if not policy.allowed:
            return policy.to_result(action)
        policy_fields = {
            "policy_code": policy.code,
            "policy_disposition": policy.disposition.value,
            "confidence": policy.confidence,
            "reference_price": policy.reference_price,
            "candidate_price": policy.candidate_price,
            "deviation_pct": policy.deviation_pct,
        }

        def with_policy(result: dict[str, Any]) -> dict[str, Any]:
            return {**result, **policy_fields}

        with self._state_lock:
            if target_symbol in self._reduction_intents and action != "CANCEL_PENDING":
                return with_policy({
                    "executed": False,
                    "status": "REDUCING",
                    "order_id": None,
                    "action": action,
                    "reason": "deterministic position reduction is already active",
                })

        with self._state_lock:
            if self._trigger_in_flight:
                return with_policy(
                    {"executed": False, "status": "BUSY", "order_id": None, "action": ""}
                )
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
                    return with_policy({
                        "executed": cancel_status.status == "CANCELLED",
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_PENDING",
                    })
                replacement_action = str(decision.get("replacement_action") or "NONE").upper()
                mapped_action = _LLM_ORDER_ACTION_MAP.get(replacement_action)
                if mapped_action is None:
                    return with_policy({
                        "executed": False,
                        "status": "UNKNOWN_ACTION",
                        "order_id": None,
                        "action": "CANCEL_REPLACE",
                    })
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
                    return with_policy(skipped)
                session_skipped = self._check_trading_session(
                    mapped_action, symbol=target_symbol, market=target_market
                )
                if session_skipped is not None:
                    return with_policy(session_skipped)
                cancel_status = self._trade_svc.cancel_pending_order_for_symbol(
                    target_symbol,
                    risk=self.risk,
                    notifier=self.notifier,
                    restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
                    notify_risk_event=self.notifier.notify_risk_event,
                )
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return with_policy({
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": cancel_status.broker_order_id or None,
                        "action": "CANCEL_REPLACE",
                    })
                return with_policy(self._execute_llm_trade_action(
                    mapped_action,
                    proposed_price,
                    allow_loss_exit=replacement_action in _LLM_STOP_LOSS_ACTIONS,
                    symbol=target_symbol,
                    market=target_market,
                    engine=target_engine,
                    reference_quote=reference_quote,
                ))

            mapped_action = _LLM_ORDER_ACTION_MAP.get(action)
            if mapped_action is None:
                return with_policy(
                    {"executed": False, "status": "UNKNOWN_ACTION", "order_id": None, "action": action}
                )
            pending = self._trade_svc.pending_order_for(target_symbol)
            skipped = self._precheck_llm_action(
                mapped_action,
                decision.get("order_price"),
                pending,
                symbol=target_symbol,
                engine=target_engine,
            )
            if skipped is not None:
                return with_policy(skipped)
            if pending is not None:
                session_skipped = self._check_trading_session(
                    mapped_action, symbol=target_symbol, market=target_market
                )
                if session_skipped is not None:
                    return with_policy(session_skipped)
                cancel_status = self._trade_svc.cancel_pending_order_for_symbol(
                    target_symbol,
                    risk=self.risk,
                    notifier=self.notifier,
                    restore_engine_snapshot=lambda snapshot: target_engine.restore(snapshot),
                    notify_risk_event=self.notifier.notify_risk_event,
                )
                replaced_order_id = cancel_status.broker_order_id or None
                if cancel_status.status not in {"CANCELLED", "NO_PENDING_ORDER"}:
                    return with_policy({
                        "executed": False,
                        "status": cancel_status.status,
                        "order_id": replaced_order_id,
                        "action": mapped_action,
                    })
                result = self._execute_llm_trade_action(
                    mapped_action,
                    decision.get("order_price"),
                    allow_loss_exit=action in _LLM_STOP_LOSS_ACTIONS,
                    symbol=target_symbol,
                    market=target_market,
                    engine=target_engine,
                    reference_quote=reference_quote,
                )
                if replaced_order_id is not None:
                    result["replaced_order_id"] = replaced_order_id
                return with_policy(result)
            return with_policy(self._execute_llm_trade_action(
                mapped_action,
                decision.get("order_price"),
                allow_loss_exit=action in _LLM_STOP_LOSS_ACTIONS,
                symbol=target_symbol,
                market=target_market,
                engine=target_engine,
                reference_quote=reference_quote,
            ))
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
        reference_quote: Quote | None = None,
    ) -> dict[str, Any]:
        risk_result = self.risk.check()
        if not risk_result.approved and not self._risk_rejection_allows_action(action):
            return {"executed": False, "status": "RISK_REJECTED", "order_id": None, "action": action}
        if not risk_result.approved:
            logger.info("allowing LLM position-reducing %s despite risk rejection: %s", action, risk_result.reason)

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

        quote = self._quote_for_llm_order(target_symbol, price, reference_quote=reference_quote)
        if quote is None:
            target_engine.restore(engine_snapshot)
            return {"executed": False, "status": "NO_QUOTE", "order_id": None, "action": action}

        try:
            llm_decision = _QuoteTriggerDecision(
                result=TriggerResult(
                    triggered=True,
                    action=action,
                    description="LLM trade action",
                ),
                trigger_symbol=target_symbol,
                trigger_engine=target_engine,
                trigger_market=target_market,
                allow_loss_exit=allow_loss_exit,
                reduce_only=action in _POSITION_REDUCING_ACTIONS,
                reduction_cause=(
                    "LLM_STOP_LOSS" if allow_loss_exit else "LLM"
                ),
            )
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
                reduce_only=llm_decision.reduce_only,
                execution_context=self._execution_ledger_context(
                    llm_decision,
                    quote,
                    "LLM trade action",
                ),
            )
        except Exception:
            target_engine.restore(engine_snapshot)
            reason = (
                f"{_ORDER_SUBMISSION_UNCERTAIN_PREFIX} LLM order submission "
                f"outcome is unknown for symbol={target_symbol} action={action}"
            )
            self.risk.pause(reason, auto_resumable=False)
            self._set_last_action_message(reason)
            self._persist_risk_pause_best_effort()
            try:
                self._record_risk_event(reason)
            except Exception:
                logger.exception("failed to record uncertain LLM order submission")
            logger.exception("LLM order submission outcome is unknown; trading paused")
            return {
                "executed": False,
                "status": "ORDER_SUBMISSION_UNCERTAIN",
                "order_id": None,
                "action": action,
            }
        if order_status is None:
            target_engine.restore(engine_snapshot)
            return {"executed": False, "status": "NO_ORDER", "order_id": None, "action": action}
        if order_status.status in {"SKIPPED", "REJECTED", "CANCELLED"}:
            target_engine.restore(engine_snapshot)
        elif order_status.status == "FILLED" and action in _POSITION_REDUCING_ACTIONS:
            self._on_reduction_fill(
                target_symbol,
                action,
                Decimal(str(order_status.executed_quantity or 0)),
            )
        result = {
            "executed": order_status.status in {"FILLED", "SUBMITTED", "PARTIAL_FILLED"},
            "status": order_status.status,
            "order_id": order_status.broker_order_id or None,
            "action": action,
        }
        if result["executed"]:
            side = self._broker_side_for_action(action)
            self._last_llm_action_at[(target_symbol, side)] = time.monotonic()
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

    def _trusted_quote_for_llm_policy(self, symbol: str) -> Quote | None:
        try:
            quotes = self.broker.get_quotes([symbol])
        except Exception:
            logger.exception("failed to fetch trusted quote for LLM order policy")
            return None
        quote = quotes[0] if quotes else None
        if quote is None or str(quote.symbol).upper() != symbol.upper():
            return None
        try:
            bid = float(quote.bid)
            ask = float(quote.ask)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(bid) or not math.isfinite(ask) or bid <= 0 or ask < bid:
            return None
        reference = (bid + ask) / 2
        if reference <= 0 or (ask - bid) / reference >= _QUOTE_SPREAD_THRESHOLD_PCT:
            return None
        return quote

    def _quote_for_llm_order(
        self,
        symbol: str,
        price: Any = None,
        *,
        reference_quote: Quote | None = None,
    ) -> Quote | None:
        override_price = self._coerce_positive_float(price)
        quote = reference_quote
        if quote is None:
            quote = self._trusted_quote_for_llm_policy(symbol)
        if quote is None:
            return None
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
                "protective_exit_permitted": self.risk.protective_exit_permitted,
            }
            data["runner_running"] = self.is_running
            data["last_action_message"] = self.last_action_message
            data["trading_session_mode"] = self._get_trading_session_mode()
            data["is_trading_hours"] = is_trading_hours(self.engine.params.market)
            execution_state, reduction_reason, reduction_started_at = self.execution_state()
            data["execution_state"] = execution_state
            data["reduction_reason"] = reduction_reason
            data["reduction_started_at"] = (
                reduction_started_at.isoformat() if reduction_started_at is not None else None
            )
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
        last_price = float(quote.last_price)
        bid = float(quote.bid)
        ask = float(quote.ask)
        quality = self._evaluate_quote_quality(
            {
                "last_price": last_price,
                "bid": bid,
                "ask": ask,
                "timestamp": quote.timestamp,
            }
        )
        trusted = bool(
            quality["price_positive"]
            and quality["spread_reasonable"]
            and quality["last_bbo_consistent"]
            and quality["source_timestamp_fresh"]
        )
        self._remember_symbol_runtime_quote(quote, now, trusted=trusted)
        # This timestamp is the health signal for the trading symbol. A fresh
        # watchlist quote must not mask a silent primary feed or suppress the
        # primary's active refresh loop.
        if trusted and quote.symbol == self.engine.params.symbol:
            self._last_quote_at = time.monotonic()
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

    def fresh_market_price(
        self,
        symbol: str | None = None,
        *,
        max_age_seconds: float = 30.0,
    ) -> float | None:
        """Return a recent, executable-quality price observed by this process."""
        requested_symbol = (symbol or self.engine.params.symbol or "").strip().upper()
        if not requested_symbol or not math.isfinite(max_age_seconds) or max_age_seconds <= 0:
            return None
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=max_age_seconds)
        with self._state_lock:
            runtime = self._symbol_runtimes.get(requested_symbol)
            candidates = runtime.recent_quotes if runtime is not None else self._recent_quotes
            for item in reversed(candidates):
                if str(item.get("symbol") or "").upper() != requested_symbol:
                    continue
                observed_at = item.get("observed_at")
                if not isinstance(observed_at, datetime):
                    continue
                normalized_observed_at = (
                    observed_at.replace(tzinfo=timezone.utc)
                    if observed_at.tzinfo is None
                    else observed_at.astimezone(timezone.utc)
                )
                if normalized_observed_at < cutoff:
                    break
                quality = self._evaluate_quote_quality(item)
                if (
                    quality["price_positive"]
                    and quality["spread_reasonable"]
                    and quality["last_bbo_consistent"]
                    and quality["source_timestamp_fresh"]
                ):
                    return float(item["last_price"])
        return None

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

    def _validate_final_order_quote(
        self,
        broker: BrokerGateway,
        symbol: str,
        action: str,
        limit_price: Decimal,
    ) -> FinalOrderQuoteCheckResult | str:
        quotes = broker.get_quotes([symbol])
        if len(quotes) != 1 or quotes[0].symbol != symbol:
            return "fresh quote for the submitted symbol is unavailable"
        quote = quotes[0]
        quality = self._evaluate_quote_quality(
            {
                "last_price": quote.last_price,
                "bid": quote.bid,
                "ask": quote.ask,
                "timestamp": quote.timestamp,
            }
        )
        if not all(
            bool(quality[name])
            for name in (
                "price_positive",
                "spread_reasonable",
                "last_bbo_consistent",
                "source_timestamp_fresh",
            )
        ):
            return "fresh executable quote failed the final quality gate"
        executable = Decimal(
            str(
                quote.ask
                if action in {"BUY", "BUY_TO_COVER"}
                else quote.bid
            )
        )
        if not executable.is_finite() or executable <= 0:
            return "fresh executable BBO price is unavailable"
        deviation = abs(limit_price - executable) / executable
        if deviation > Decimal(str(_QUOTE_LAST_BBO_DEVIATION_THRESHOLD_PCT)):
            return (
                "submitted limit price deviates from fresh executable BBO by "
                f"{float(deviation) * 100:.2f}%"
            )
        return FinalOrderQuoteCheckResult(executable_price=executable)

    @staticmethod
    def _evaluate_quote_quality(recent: dict[str, Any] | None) -> dict[str, Any]:
        if recent is None:
            return {
                "has_quote": False,
                "price_positive": False,
                "spread_reasonable": False,
                "last_bbo_consistent": False,
                "source_timestamp_fresh": False,
            }
        last_price = float(recent.get("last_price", 0))
        bid = float(recent.get("bid", 0))
        ask = float(recent.get("ask", 0))
        price_positive = math.isfinite(last_price) and last_price > 0
        spread_reasonable = False
        last_bbo_consistent = False
        if (
            price_positive
            and math.isfinite(bid)
            and math.isfinite(ask)
            and bid > 0
            and ask > 0
            and ask >= bid
        ):
            midpoint = (bid + ask) / 2
            spread_pct = (ask - bid) / midpoint if midpoint > 0 else math.inf
            spread_reasonable = spread_pct < _QUOTE_SPREAD_THRESHOLD_PCT
            last_bbo_consistent = (
                abs(last_price - midpoint) / midpoint
                < _QUOTE_LAST_BBO_DEVIATION_THRESHOLD_PCT
            )
        source_timestamp_fresh = AppRunner._quote_source_timestamp_is_fresh(
            recent.get("timestamp")
        )
        return {
            "has_quote": True,
            "price_positive": price_positive,
            "spread_reasonable": spread_reasonable,
            "last_bbo_consistent": last_bbo_consistent,
            "source_timestamp_fresh": source_timestamp_fresh,
            "last_price": last_price,
            "bid": bid,
            "ask": ask,
        }

    @staticmethod
    def _quote_source_timestamp_is_fresh(value: object) -> bool:
        raw = str(value or "").strip()
        if not raw:
            return False
        try:
            if raw.replace(".", "", 1).isdigit():
                numeric = float(raw)
                if numeric > 10_000_000_000:
                    numeric /= 1000
                source_time = datetime.fromtimestamp(numeric, tz=timezone.utc)
            else:
                source_time = datetime.fromisoformat(raw.replace("Z", "+00:00"))
                if source_time.tzinfo is None:
                    source_time = source_time.replace(tzinfo=timezone.utc)
                else:
                    source_time = source_time.astimezone(timezone.utc)
        except (ValueError, OverflowError, OSError):
            return False
        age_seconds = (datetime.now(timezone.utc) - source_time).total_seconds()
        return -5.0 <= age_seconds <= _QUOTE_SOURCE_MAX_AGE_SECONDS

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
        with self._trade_svc.submission_guard():
            changed, broker_orders = self._sync_today_orders_from_broker_serialized(force=force)
        self._enrich_broker_order_costs(broker_orders)
        return changed

    def _sync_today_orders_from_broker_serialized(
        self,
        *,
        force: bool = False,
    ) -> tuple[int, Sequence[object]]:
        with self._state_lock:
            now = time.monotonic()
            if (
                not force
                and self._last_order_sync_at > 0
                and now - self._last_order_sync_at < self._order_sync_interval_seconds
            ):
                return 0, ()
            self._last_order_sync_at = now
            self._last_order_sync_succeeded = False

        try:
            broker_orders = self.broker.get_today_orders()
        except Exception as exc:
            logger.warning("broker today order sync failed: %s", exc)
            with self._state_lock:
                self._last_order_sync_succeeded = False
            self._latch_live_order_reconciliation(
                self._trade_svc.pending_order_inventory(),
                [_ORDER_SNAPSHOT_FETCH_ISSUE],
            )
            return 0, ()
        broker_representation_issues: dict[int, str] = {}
        broker_live_inventory: dict[str, list[str]] = {}
        for index, broker_order in enumerate(broker_orders):
            status = str(
                getattr(broker_order, "status", "SUBMITTED") or "SUBMITTED"
            ).upper()
            if status in _LIVE_ORDER_STATUSES:
                order_id = str(
                    getattr(broker_order, "broker_order_id", "") or ""
                ).strip()
                symbol = str(
                    getattr(broker_order, "symbol", "") or ""
                ).strip().upper()
                broker_live_inventory.setdefault(
                    symbol or "<missing-symbol>",
                    [],
                ).append(order_id or f"<missing-id:broker-{index}>")
            issue = self._live_order_representation_issue(
                broker_order,
                source=f"broker live order[{index}]",
            )
            if issue is not None:
                broker_representation_issues[index] = issue
        changed = 0
        live_inventory: dict[str, list[str]] = {}
        representation_issues = list(broker_representation_issues.values())
        try:
            with self._order_persistence_lock:
                with self._db_session() as db:
                    for index, broker_order in enumerate(broker_orders):
                        if index in broker_representation_issues:
                            continue
                        if self._upsert_broker_order(db, broker_order):
                            changed += 1
                    # SessionLocal disables autoflush. Without an explicit flush, the
                    # live-order query below can re-read the pre-sync SUBMITTED row and
                    # manufacture an in-memory pending for an order that the broker just
                    # reported FILLED. On startup that stale pending prevents offline
                    # fill recovery from completing REDUCING.
                    db.flush()
                    semantic_broker_orders = {
                        str(getattr(order, "broker_order_id", "") or ""): order
                        for index, order in enumerate(broker_orders)
                        if index not in broker_representation_issues
                        and (
                            str(
                                getattr(order, "status", "SUBMITTED")
                                or "SUBMITTED"
                            ).upper()
                            in _LIVE_ORDER_STATUSES
                            or self._has_terminal_execution(order)
                        )
                        and str(getattr(order, "broker_order_id", "") or "")
                    }
                    if semantic_broker_orders:
                        try:
                            submitted_events = (
                                db.query(TradeEvent)
                                .filter(
                                    TradeEvent.event_type == "ORDER_SUBMITTED",
                                    TradeEvent.broker_order_id.in_(
                                        sorted(semantic_broker_orders)
                                    ),
                                )
                                .all()
                            )
                            submitted_ids = {
                                str(event.broker_order_id)
                                for event in submitted_events
                                if self._submission_event_matches_order(
                                    event,
                                    semantic_broker_orders.get(
                                        str(event.broker_order_id)
                                    ),
                                )
                            }
                        except Exception:
                            logger.exception(
                                "failed to prove local provenance for terminal executions"
                            )
                            submitted_ids = set()
                        for order_id in sorted(
                            set(semantic_broker_orders) - submitted_ids
                        ):
                            representation_issues.append(
                                "broker live or terminal order "
                                f"id={order_id} lacks local submission provenance"
                            )
                    representation_issues.extend(self._load_pending_orders(db))
                    db_inventory, db_issues = self._live_order_inventory_from_db(db)
                    representation_issues.extend(db_issues)
                    live_inventory = self._merge_live_order_inventories(
                        broker_live_inventory,
                        db_inventory,
                        self._trade_svc.pending_order_inventory(),
                    )
                    if self._resume_pending_timeout_pause_if_filled(db):
                        changed += 1
                    if changed:
                        db.commit()
        except Exception:
            logger.exception("broker today order sync could not be persisted")
            self._latch_live_order_reconciliation(
                self._trade_svc.pending_order_inventory(),
                ["cannot persist broker order reconciliation"],
            )
            return 0, ()
        self._latch_live_order_reconciliation(
            live_inventory,
            sorted(set(representation_issues)),
        )
        with self._state_lock:
            self._last_order_sync_succeeded = not representation_issues
        self._sync_risk_from_order_ledger()
        return changed, broker_orders

    def _enrich_broker_order_costs(self, broker_orders: Sequence[object]) -> None:
        """Best-effort refresh for charges that settle after the fill."""
        status_reader = getattr(self.broker, "get_order_status", None)
        if not callable(status_reader):
            return
        filled_ids = [
            str(getattr(order, "broker_order_id", "") or "")
            for order in broker_orders
            if str(getattr(order, "status", "") or "").upper() == "FILLED"
            and str(getattr(order, "broker_order_id", "") or "")
        ]
        if not filled_ids:
            return
        with self._db_session() as db:
            missing_ids = {
                str(order.broker_order_id)
                for order in db.query(OrderRecord).filter(
                    OrderRecord.broker_order_id.in_(filled_ids),
                    OrderRecord.actual_fee.is_(None),
                ).all()
            }
        now = time.monotonic()
        eligible_ids = [
            order_id
            for order_id in sorted(missing_ids)
            if now >= self._fee_enrichment_next_retry_at.get(order_id, 0.0)
        ][: self._fee_enrichment_batch_size]
        for order_id in eligible_ids:
            try:
                detail = status_reader(order_id)
                metadata = {
                    name: value
                    for name in (
                        "actual_fee",
                        "fee_currency",
                        "broker_submitted_at",
                        "broker_updated_at",
                    )
                    if (value := getattr(detail, name, None)) is not None
                    and value != ""
                }
                if "actual_fee" not in metadata:
                    self._schedule_fee_enrichment_retry(order_id, now)
                    continue
                metadata["fee_source"] = "ACTUAL"
                self._update_order_status(
                    order_id,
                    str(getattr(detail, "status", "FILLED") or "FILLED"),
                    getattr(detail, "broker_updated_at", None),
                    float(getattr(detail, "executed_quantity", 0) or 0),
                    float(getattr(detail, "executed_price", 0) or 0),
                    metadata,
                )
                self._fee_enrichment_attempts.pop(order_id, None)
                self._fee_enrichment_next_retry_at.pop(order_id, None)
            except Exception:
                self._schedule_fee_enrichment_retry(order_id, now)
                logger.warning(
                    "broker charges are not yet available for order %s",
                    order_id,
                    exc_info=True,
                )

    def _schedule_fee_enrichment_retry(self, order_id: str, now: float) -> None:
        attempts = self._fee_enrichment_attempts.get(order_id, 0) + 1
        self._fee_enrichment_attempts[order_id] = attempts
        self._fee_enrichment_next_retry_at[order_id] = now + min(
            3600.0,
            60.0 * (2 ** min(attempts - 1, 6)),
        )

    def verify_operational_resume(
        self,
        *,
        require_complete_pnl: bool = True,
    ) -> tuple[bool, str]:
        """Prove broker/order state is coherent before changing pause access."""
        with self._state_lock:
            if self.risk.kill_switch:
                self._unknown_submission_proof_reason = ""
                self._unknown_submission_proof_at = 0.0
                return False, "kill switch must be disabled before trading can resume"
            if not self.risk.paused:
                self._unknown_submission_proof_reason = ""
                self._unknown_submission_proof_at = 0.0
                return True, ""
            pause_reason = self.risk.pause_reason
            paused_at = self.risk.paused_at
            previous_proof_reason = self._unknown_submission_proof_reason
            previous_proof_at = self._unknown_submission_proof_at
            self._unknown_submission_proof_reason = ""
            self._unknown_submission_proof_at = 0.0
            if self._trigger_in_flight:
                return False, "an order trigger is still in flight"

        delayed_resume_proof = pause_reason.startswith(
            (
                _ORDER_SUBMISSION_UNCERTAIN_PREFIX,
                _ORDER_RECONCILIATION_UNCERTAIN_PREFIX,
            )
        )
        if delayed_resume_proof:
            proof_subject = (
                "unknown order submission"
                if pause_reason.startswith(_ORDER_SUBMISSION_UNCERTAIN_PREFIX)
                else "uncertain order reconciliation"
            )
            if paused_at is None:
                return False, f"{proof_subject} pause time is unavailable"
            normalized_paused_at = self._as_utc(paused_at)
            pause_age = datetime.now(timezone.utc) - normalized_paused_at
            remaining = (
                _UNKNOWN_SUBMISSION_RESUME_GRACE_SECONDS
                - pause_age.total_seconds()
            )
            if remaining > 0:
                return (
                    False,
                    f"{proof_subject} is still inside its broker "
                    f"settlement grace period ({math.ceil(remaining)}s remaining)",
                )

        self.sync_today_orders_from_broker(force=True)
        with self._state_lock:
            order_sync_succeeded = self._last_order_sync_succeeded
            current_pause_reason = self.risk.pause_reason
            unresolved_live_order_ids = list(self._unresolved_live_order_ids)
            representation_issues = list(self._unrepresentable_live_order_issues)
        pending_order_ids = sorted(
            set(self._trade_svc.pending_order_ids())
            | set(unresolved_live_order_ids)
        )
        if pending_order_ids:
            return (
                False,
                "live or unresolved orders still exist: " + ", ".join(pending_order_ids),
            )
        if representation_issues:
            return (
                False,
                "live broker orders cannot be represented safely: "
                + "; ".join(representation_issues),
            )
        if not order_sync_succeeded:
            return False, "broker order reconciliation has not completed successfully"
        if current_pause_reason != pause_reason:
            return False, "operational pause reason changed during reconciliation"
        try:
            positions = self.broker.get_positions()
        except Exception:
            logger.exception("cannot verify broker positions before operational resume")
            return False, "broker positions could not be verified"

        active_positions = [
            position
            for position in positions
            if Decimal(str(position.quantity)) > 0
        ]
        primary_symbol = self.engine.params.symbol
        unexpected_symbols = sorted(
            {
                position.symbol
                for position in active_positions
                if position.symbol != primary_symbol
            }
        )
        if unexpected_symbols:
            return (
                False,
                "broker exposure exists outside the primary strategy: "
                + ", ".join(unexpected_symbols),
            )

        primary_positions = [
            position for position in active_positions if position.symbol == primary_symbol
        ]
        primary_sides = {str(position.side).upper() for position in primary_positions}
        if len(primary_sides) > 1 or not primary_sides.issubset({"LONG", "SHORT"}):
            return False, "broker position side is ambiguous for the primary strategy"
        if pause_reason.startswith(_REDUCTION_SETTLEMENT_UNCERTAIN_PREFIX) and primary_positions:
            return False, "reduction settlement is still not reflected by broker positions"

        try:
            with self._db_session() as db:
                completed = self._reconcile_tracked_entries_with_broker(
                    db,
                    source="manual_operational_resume",
                    position_snapshot=positions,
                )
        except Exception:
            logger.exception("position reconciliation failed before operational resume")
            return False, "broker positions could not be reconciled durably"

        for symbol, intent in completed:
            self._complete_reduction(
                symbol,
                cause=intent.cause,
                reason=intent.reason,
            )

        with self._state_lock:
            unsettled_positions = sorted(
                self._unsettled_position_symbols
                | set(self._post_fill_expectations)
            )
        if unsettled_positions:
            return (
                False,
                "recent fill settlement is still unproven for: "
                + ", ".join(unsettled_positions),
            )

        broker_quantity = sum(
            (Decimal(str(position.quantity)) for position in primary_positions),
            Decimal("0"),
        )
        broker_side = next(iter(primary_sides), "")
        tracked = self._trade_svc.tracked_position(primary_symbol)
        if broker_quantity > 0 and (
            tracked is None
            or tracked.quantity != broker_quantity
            or tracked.side != broker_side
            or tracked.avg_price <= 0
            or tracked.opened_at is None
        ):
            return False, "primary broker position is not durably tracked"
        if broker_quantity <= 0 and tracked is not None:
            return False, "local tracked position remains after broker reported flat"

        has_long = broker_side == "LONG" and broker_quantity > 0
        has_short = broker_side == "SHORT" and broker_quantity > 0
        with self._state_lock:
            self.engine.sync_state(has_long, has_short)
        try:
            with self._db_session() as db:
                self._state_svc.persist(db, self.engine, self.risk)
        except Exception:
            logger.exception("engine state could not be persisted before operational resume")
            return False, "reconciled engine state could not be persisted"

        if require_complete_pnl:
            try:
                trade_day = self._market_trade_day()
                with self._db_session() as db:
                    pnl_result = DailyPnlService(db).calculate(
                        trade_day=trade_day,
                        to_trade_day=self._market_trade_day_for,
                        fee_rate_us=self.engine.params.fee_rate_us,
                        fee_rate_hk=self.engine.params.fee_rate_hk,
                    )
            except Exception:
                logger.exception("cannot verify realized PnL before operational resume")
                return False, "realized PnL reconciliation could not be verified"
            if not pnl_result.is_complete:
                return (
                    False,
                    "realized PnL ledger remains incomplete for trade day "
                    f"{pnl_result.trade_day}",
                )

        if delayed_resume_proof:
            proof_now = time.monotonic()
            proof_is_old_enough = (
                previous_proof_reason == pause_reason
                and previous_proof_at > 0
                and proof_now - previous_proof_at
                >= _UNKNOWN_SUBMISSION_SECOND_PROOF_SECONDS
            )
            if not proof_is_old_enough:
                with self._state_lock:
                    if self.risk.paused and self.risk.pause_reason == pause_reason:
                        self._unknown_submission_proof_reason = pause_reason
                        self._unknown_submission_proof_at = (
                            previous_proof_at
                            if previous_proof_reason == pause_reason
                            and previous_proof_at > 0
                            else proof_now
                        )
                return (
                    False,
                    "first coherent empty broker proof recorded; a second proof "
                    f"is required after {_UNKNOWN_SUBMISSION_SECOND_PROOF_SECONDS:.0f}s",
                )
        return True, ""

    def _market_trade_day(self):
        return trade_day_for(self.engine.params.market)

    def _market_trade_day_for(self, instant) -> Any:
        return trade_day_for(self.engine.params.market, instant)

    def _sync_risk_from_order_ledger(self) -> bool:
        # A live partial fill can still be finalized by TradeExecutionService.
        # Replaying it from the DB first would count the same realized loss
        # twice. Once all pending orders are terminal, ledger replay becomes
        # the single durable reconciliation source again.
        with self._state_lock:
            if self._trigger_in_flight or self._trade_svc.has_pending_order:
                return False
        trade_day = self._market_trade_day()
        try:
            with self._db_session() as db:
                result = DailyPnlService(db).calculate(
                    trade_day=trade_day,
                    to_trade_day=self._market_trade_day_for,
                    fee_rate_us=self.engine.params.fee_rate_us,
                    fee_rate_hk=self.engine.params.fee_rate_hk,
                )
        except Exception:
            logger.exception("failed to sync realized daily pnl from order ledger")
            if self._defer_incomplete_pnl_latch:
                logger.warning(
                    "deferring failed order-ledger pause until startup position "
                    "reconciliation finishes for trade day %s",
                    trade_day,
                )
                return False
            self._latch_pnl_reconciliation_uncertain(trade_day)
            return False
        if not result.is_complete:
            if self._defer_incomplete_pnl_latch:
                logger.warning(
                    "deferring incomplete order-ledger pause until startup "
                    "position reconciliation finishes for trade day %s",
                    result.trade_day,
                )
                return False
            self._latch_pnl_reconciliation_uncertain(result.trade_day)
            return False

        with self._state_lock:
            old_daily_pnl = self.risk.daily_pnl
            old_consecutive_losses = self.risk.consecutive_losses
            old_daily_pnl_date = self.risk.daily_pnl_date
            new_pnl, new_losses = DailyPnlService.reconcile_risk_state(
                old_daily_pnl,
                old_consecutive_losses,
                old_daily_pnl_date,
                result,
            )
            guarded_replay = new_pnl != result.realized_pnl or new_losses != result.consecutive_losses
            if guarded_replay:
                warning_key = (
                    result.trade_day,
                    old_daily_pnl,
                    old_consecutive_losses,
                    result.realized_pnl,
                    result.consecutive_losses,
                )
                if self._last_guarded_ledger_replay != warning_key:
                    logger.warning(
                        "ledger replay would make same-day risk state more optimistic: "
                        "current pnl=%s/losses=%s, ledger pnl=%s/losses=%s; "
                        "applying pnl=%s/losses=%s",
                        old_daily_pnl,
                        old_consecutive_losses,
                        result.realized_pnl,
                        result.consecutive_losses,
                        new_pnl,
                        new_losses,
                    )
                    self._last_guarded_ledger_replay = warning_key
            else:
                self._last_guarded_ledger_replay = None
            changed = (
                abs(old_daily_pnl - new_pnl) > 1e-9
                or old_consecutive_losses != new_losses
                or old_daily_pnl_date != result.trade_day
            )
            if not changed:
                return False

            self.risk.replace_daily_pnl(
                new_pnl,
                new_losses,
                result.trade_day,
            )

        with self._db_session() as db:
            self._state_svc.persist_risk(db, self.risk, symbol=self.engine.params.symbol)
        logger.info(
            "synced realized daily pnl from order ledger: "
            "ledger_pnl=%s applied_pnl=%s consecutive_losses=%s trades=%s",
            result.realized_pnl,
            new_pnl,
            new_losses,
            len(result.trades),
        )
        return True

    def _latch_pnl_reconciliation_uncertain(self, trade_day: object) -> None:
        reason = (
            f"{_PNL_RECONCILIATION_UNCERTAIN_PREFIX} incomplete order ledger "
            f"for trade day {trade_day}; new entries remain blocked until "
            "the realized PnL can be reconciled"
        )
        latched, current_reason = self.risk.pause_unless_operational(
            reason,
            auto_resumable=False,
        )
        if not latched:
            if current_reason == reason:
                return
            logger.critical(
                "%s; preserving existing operational pause: %s",
                reason,
                current_reason,
            )
            return
        logger.critical(reason)
        self._set_last_action_message(reason)
        self._persist_risk_pause_best_effort()
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record PnL reconciliation risk")
        self._broadcast_status()

    def _upsert_broker_order(self, db, broker_order: object) -> bool:
        order_id = str(getattr(broker_order, "broker_order_id", "") or "")
        if not order_id:
            return False

        symbol = str(getattr(broker_order, "symbol", "") or "")
        side = str(getattr(broker_order, "side", "") or "")
        status = str(
            getattr(broker_order, "status", "SUBMITTED") or "SUBMITTED"
        ).upper()
        quantity = self._coerce_float(getattr(broker_order, "quantity", 0))
        price = self._coerce_float(getattr(broker_order, "price", 0))
        executed_quantity = self._coerce_optional_float(getattr(broker_order, "executed_quantity", None))
        executed_price = self._coerce_optional_float(getattr(broker_order, "executed_price", None))
        created_at = getattr(broker_order, "created_at", None) or datetime.now(timezone.utc)
        filled_at = getattr(broker_order, "filled_at", None)
        if status == "FILLED":
            executed_quantity = executed_quantity or quantity or None
            executed_price = executed_price or price or None
        if filled_at is None and float(executed_quantity or 0) > 0:
            filled_at = datetime.now(timezone.utc)

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

        old_status = str(order.status or "").upper()
        old_executed_quantity = order.executed_quantity
        old_executed_price = order.executed_price
        changed = False
        preserved_side = order.side
        if preserved_side in {"SELL_SHORT", "BUY_TO_COVER"} and side in {"SELL", "BUY"}:
            sync_side = preserved_side
        else:
            sync_side = side
        effective_status = (
            status
            if self._order_status_rank(status) > self._order_status_rank(old_status)
            or status == old_status
            else old_status
        )
        current_executed_quantity = float(order.executed_quantity or 0)
        incoming_executed_quantity = float(executed_quantity or 0)
        merged_executed_quantity = max(
            current_executed_quantity,
            incoming_executed_quantity,
        )
        merged_executed_price = order.executed_price
        if (
            executed_price is not None
            and executed_price > 0
            and incoming_executed_quantity >= current_executed_quantity
        ):
            merged_executed_price = executed_price
        updates = {
            "symbol": symbol or order.symbol,
            "side": sync_side or order.side,
            "quantity": quantity if quantity > 0 else order.quantity,
            "price": price if price > 0 else order.price,
            "status": effective_status,
            "executed_quantity": (
                merged_executed_quantity
                if merged_executed_quantity > 0
                else order.executed_quantity
            ),
            "executed_price": merged_executed_price,
        }
        for name, value in updates.items():
            if getattr(order, name) != value:
                setattr(order, name, value)
                changed = True
        execution_advanced = incoming_executed_quantity > current_executed_quantity
        execution_became_terminal = (
            effective_status in _TERMINAL_ORDER_STATUSES
            and old_status not in _TERMINAL_ORDER_STATUSES
        )
        if filled_at is not None and (
            order.filled_at is None
            or execution_advanced
            or execution_became_terminal
        ):
            order.filled_at = filled_at
            changed = True
        elif effective_status == "FILLED" and order.filled_at is None:
            order.filled_at = datetime.now(timezone.utc)
            changed = True

        accounting_fields = (
            "fill_latency_ms",
            "cost_basis_quantity",
            "gross_pnl",
            "net_pnl",
            "pnl_source",
            "pnl_fee",
            "pnl_fee_source",
            "slippage_amount",
            "slippage_bps",
        )
        accounting_before = tuple(
            getattr(order, name) for name in accounting_fields
        )
        if self._has_terminal_execution(order):
            # A locally submitted order may become terminal while the process is
            # down. Rebuild the authoritative outcome from the cost basis frozen
            # on submission before startup drops the row from pending recovery.
            self._update_execution_outcome_fields(order)
        accounting_changed = accounting_before != tuple(
            getattr(order, name) for name in accounting_fields
        )
        changed = changed or accounting_changed

        if not changed:
            return False

        db.add(order)
        event_type = self._order_event_type_for_status(effective_status)
        if event_type == "ORDER_STATUS_CHANGED" and effective_status == old_status:
            message = (
                "broker order execution changed while status remained "
                f"{effective_status}"
            )
        else:
            message = (
                f"broker order status changed from {old_status} to {effective_status}"
            )
        record_trade_event(
            db,
            event_type=event_type,
            symbol=symbol,
            broker_order_id=order_id,
            side=side,
            status=effective_status,
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
                if self._reconcile_runtime_positions():
                    self._broadcast_status()
            except Exception:
                logger.exception("error reconciling runtime broker positions")
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
        with self._trade_svc.submission_guard():
            return self._sync_engine_state_with_positions_under_submission_guard(
                force=force
            )

    def _sync_engine_state_with_positions_under_submission_guard(
        self,
        *,
        force: bool = False,
    ) -> bool:
        with self._state_lock:
            if (not self._running and not force) or self._trigger_in_flight:
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
            with self._state_lock:
                if symbol in self._unsettled_position_symbols:
                    continue
                expectation = self._post_fill_expectations.get(symbol)
            if expectation is not None:
                if not self._position_snapshot_matches_expectation(
                    symbol,
                    positions,
                    expectation,
                ):
                    reason = (
                        f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                        f"broker position has not settled after fill for {symbol}"
                    )
                    self.risk.pause(reason, auto_resumable=False)
                    self._persist_risk_pause_best_effort()
                    continue
                with self._state_lock:
                    if self._post_fill_expectations.get(symbol) is expectation:
                        self._post_fill_expectations.pop(symbol, None)
            else:
                tracked = self._trade_svc.tracked_position(symbol)
                broker_has_position = any(
                    position.symbol == symbol
                    and Decimal(str(position.quantity)) > 0
                    for position in positions
                )
                if (
                    tracked is not None
                    and not broker_has_position
                    and self._tracked_entry_is_in_settlement_grace(tracked.opened_at)
                ):
                    reason = (
                        f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                        f"broker position has not settled after fill for {symbol}"
                    )
                    self.risk.revoke_protective_exits()
                    if not (
                        self.risk.paused
                        and self.risk.pause_reason.startswith(
                            _OPERATIONAL_PAUSE_PREFIXES
                        )
                    ):
                        self.risk.pause(reason, auto_resumable=False)
                    self._persist_risk_pause_best_effort()
                    continue
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

    @staticmethod
    def _position_snapshot_matches_expectation(
        symbol: str,
        positions: list[Position],
        expectation: _PostFillExpectation,
    ) -> bool:
        try:
            active = [
                position
                for position in positions
                if position.symbol == symbol
                and Decimal(str(position.quantity)) > 0
            ]
            sides = {str(position.side).upper() for position in active}
            quantity = sum(
                (Decimal(str(position.quantity)) for position in active),
                Decimal("0"),
            )
        except Exception:
            return False
        if expectation.quantity <= 0:
            return quantity <= 0
        return sides == {expectation.side} and quantity == expectation.quantity

    @staticmethod
    def _tracked_entry_is_in_settlement_grace(opened_at: datetime | None) -> bool:
        if opened_at is None:
            return False
        normalized = (
            opened_at.replace(tzinfo=timezone.utc)
            if opened_at.tzinfo is None
            else opened_at.astimezone(timezone.utc)
        )
        age = datetime.now(timezone.utc) - normalized
        return timedelta(0) <= age < timedelta(
            seconds=_POST_FILL_SETTLEMENT_GRACE_SECONDS
        )

    def _reconcile_runtime_positions(self) -> bool:
        with self._state_lock:
            if (
                not self._running
                or self._trigger_in_flight
                or self._trade_svc.has_pending_order
            ):
                return False
            now = time.monotonic()
            if (
                self._last_tracked_reconcile_at > 0
                and now - self._last_tracked_reconcile_at
                < self._tracked_reconcile_interval_seconds
            ):
                return False
            self._last_tracked_reconcile_at = now

        with self._trade_svc.submission_guard():
            with self._state_lock:
                if (
                    not self._running
                    or self._trigger_in_flight
                    or self._trade_svc.has_pending_order
                ):
                    return False
            before = self._trade_svc.snapshot_tracked_entries()
            with self._db_session() as db:
                completed = self._reconcile_tracked_entries_with_broker(
                    db,
                    source="runtime_position_reconcile",
                )
                if self.risk.paused:
                    self._state_svc.persist(db, self.engine, self.risk)
            for symbol, intent in completed:
                self._complete_reduction(
                    symbol,
                    cause=intent.cause,
                    reason=intent.reason,
                )
            after = self._trade_svc.snapshot_tracked_entries()
            return before != after or bool(completed)

    def _auto_resume_pause_if_due(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        with self._state_lock:
            if self.risk.entry_reconciliation_pending:
                return False
            empty_snapshot_pause = (
                self.risk.paused
                and self.risk.pause_reason
                == _EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
            )
            other_operational_pause = (
                self.risk.paused
                and self.risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES)
                and not empty_snapshot_pause
            )
        if empty_snapshot_pause:
            # This operational pause must never fall through to the generic,
            # timer-only auto-resume path, even if old persisted data happened
            # to mark it auto-resumable.
            return self._auto_resume_empty_order_snapshot_pause(now)
        if other_operational_pause:
            return False
        with self._trade_svc.submission_guard():
            with self._state_lock:
                if (
                    self.risk.entry_reconciliation_pending
                    or not self.risk.paused
                    or self.risk.kill_switch
                ):
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
                reason, safety_generation = self.risk.pause_verification_snapshot()

            def persist_resumed_state() -> None:
                with self._db_session() as db:
                    self._state_svc.stage(db, self.engine, self.risk)
                    record_trade_event(
                        db,
                        event_type="RISK_AUTO_RESUMED",
                        status="RUNNING",
                        message=reason,
                        payload={"source": "auto_resume_pause"},
                    )
                    db.commit()

            try:
                resumed = self.risk.resume_if_pause_reason(
                    reason,
                    expected_generation=safety_generation,
                    on_resumed=persist_resumed_state,
                )
            except Exception:
                logger.exception("transient auto-resume could not be persisted")
                self._persist_risk_pause_best_effort()
                self._broadcast_status()
                return False
            if not resumed:
                return False
        logger.info("auto-resumed trading after transient pause: %s", reason)
        self._broadcast_status()
        return True

    def _auto_resume_empty_order_snapshot_pause(self, now: datetime) -> bool:
        """Recover the one reconciliation pause that has no order ambiguity."""
        with self._state_lock:
            if (
                not self.risk.paused
                or self.risk.kill_switch
                or self.risk.pause_reason
                != _EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
            ):
                return False
            paused_at = self.risk.paused_at
        if paused_at is None:
            return False
        if paused_at.tzinfo is None:
            paused_at = paused_at.replace(tzinfo=timezone.utc)
        if now - paused_at < timedelta(
            seconds=_UNKNOWN_SUBMISSION_RESUME_GRACE_SECONDS
        ):
            return False

        # Keep order submission serialized until the verified resume is durable.
        # resume_after_verification supplies the existing two independent empty
        # broker proofs, position/tracked-entry checks, and safety-generation CAS.
        with self._trade_svc.submission_guard():
            with self._state_lock:
                if (
                    not self.risk.paused
                    or self.risk.kill_switch
                    or self.risk.pause_reason
                    != _EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON
                ):
                    return False
            def persist_resumed_state() -> None:
                with self._db_session() as db:
                    self._state_svc.stage(db, self.engine, self.risk)
                    record_trade_event(
                        db,
                        event_type="RISK_AUTO_RESUMED",
                        status="RUNNING",
                        message=_EMPTY_ORDER_SNAPSHOT_RECONCILIATION_REASON,
                        payload={
                            "source": "verified_empty_order_snapshot_reconciliation",
                        },
                    )
                    db.commit()

            try:
                resumed, error = self.resume_after_verification(
                    on_resumed=persist_resumed_state,
                )
            except Exception:
                logger.exception(
                    "verified empty-order snapshot auto-resume could not be persisted"
                )
                self._persist_risk_pause_best_effort()
                self._broadcast_status()
                return False
            if not resumed:
                if error:
                    logger.info(
                        "verified empty-order snapshot auto-resume deferred: %s",
                        error,
                    )
                return False

        logger.info(
            "auto-resumed trading after verified empty order snapshot recovery"
        )
        self._broadcast_status()
        return True

    def _resume_pending_timeout_pause_if_filled(self, db: Session) -> bool:
        with self._state_lock:
            if not self.risk.paused or self.risk.kill_switch:
                return False
            pause_reason = self.risk.pause_reason
        match = _PENDING_TIMEOUT_PAUSE_RE.search(pause_reason)
        if match is None:
            return False
        order_id = match.group("order_id")
        order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == order_id).first()
        if order is None or order.status != "FILLED" or float(order.executed_quantity or 0) <= 0:
            return False

        message = f"pending timeout resolved by broker fill {order_id}"
        with self._trade_svc.submission_guard():
            with self._state_lock:
                if (
                    self.risk.entry_reconciliation_pending
                    or self.risk.pause_reason != pause_reason
                ):
                    return False
                _, safety_generation = self.risk.pause_verification_snapshot()

            def persist_resumed_state() -> None:
                self._state_svc.stage(db, self.engine, self.risk)
                record_trade_event(
                    db,
                    event_type="RISK_AUTO_RESUMED",
                    status="RUNNING",
                    message=message,
                    payload={
                        "source": "pending_timeout_fill_reconcile",
                        "order_id": order_id,
                    },
                )
                db.commit()

            try:
                resumed = self.risk.resume_if_pause_reason(
                    pause_reason,
                    expected_generation=safety_generation,
                    on_resumed=persist_resumed_state,
                )
            except Exception:
                db.rollback()
                raise
            if not resumed:
                return False
        logger.info(message)
        self._broadcast_status()
        return True

    def _reduction_intent_for_quote_locked(
        self,
        quote: Quote,
        engine: StrategyEngine,
        market: str,
    ) -> tuple[_ReductionIntent | None, bool, bool]:
        existing = self._reduction_intents.get(quote.symbol)
        tracked = self._trade_svc.tracked_position(quote.symbol)
        if tracked is None:
            if existing is not None and engine.state == EngineState.FLAT:
                # Durable REDUCING state may only be cleared after a successful
                # broker position snapshot or a confirmed fill. Local FLAT is
                # not proof that settlement completed after a restart.
                return existing, False, False
            if engine.state != EngineState.SHORT:
                return existing, False, False

        if existing is not None:
            if tracked is not None:
                engine.sync_state(
                    has_long_position=tracked.side == "LONG",
                    has_short_position=tracked.side == "SHORT",
                )
                expected_action = "SELL" if tracked.side == "LONG" else "BUY_TO_COVER"
            else:
                expected_action = (
                    "BUY_TO_COVER" if engine.state == EngineState.SHORT else "SELL"
                )
            if existing.action != expected_action:
                corrected = _ReductionIntent(
                    action=expected_action,
                    cause=existing.cause,
                    reason=(
                        f"{existing.reason}; action reconciled to broker side"
                    ),
                    trigger_price=existing.trigger_price,
                    started_at=existing.started_at,
                )
                return corrected, True, False
            return existing, False, False

        if tracked is not None:
            side = tracked.side
            quantity = float(tracked.quantity)
            avg_price = float(tracked.avg_price)
            opened_at = tracked.opened_at
        else:
            side = "SHORT"
            quantity = 1.0
            avg_price = 0.0
            opened_at = None

        executable_price = (
            float(quote.bid)
            if side == "LONG" and float(quote.bid) > 0
            else float(quote.ask)
            if side == "SHORT" and float(quote.ask) > 0
            else float(quote.last_price)
        )
        if side == "LONG":
            unrealized_pnl = (executable_price - avg_price) * quantity
        else:
            unrealized_pnl = (avg_price - executable_price) * quantity
        decision = evaluate_exit_policy(
            config=ExitPolicyConfig(
                stop_loss_pct=engine.params.stop_loss_pct,
                max_holding_minutes=engine.params.max_holding_minutes,
            ),
            position=PositionExitContext(
                symbol=quote.symbol,
                side=side,
                quantity=quantity,
                avg_entry_price=avg_price,
                opened_at=opened_at,
            ),
            quote=ExitQuote(
                last=float(quote.last_price),
                bid=float(quote.bid),
                ask=float(quote.ask),
            ),
            now=datetime.now(timezone.utc),
            in_flatten_window=is_closing_window(
                market,
                engine.params.flatten_minutes_before_close,
            ),
            combined_daily_pnl=float(self.risk.daily_pnl) + unrealized_pnl,
            max_daily_loss=float(self.risk.config.max_daily_loss or 0),
        )
        if decision is None:
            return None, False, False
        intent = self._intent_from_reduction_decision(decision)
        return intent, True, False

    @staticmethod
    def _intent_from_reduction_decision(decision: ReductionDecision) -> _ReductionIntent:
        return _ReductionIntent(
            action=decision.action,
            cause=decision.cause.value,
            reason=decision.reason,
            trigger_price=decision.trigger_price,
            started_at=datetime.now(timezone.utc),
        )

    def _restore_reduction(self, db: Session) -> None:
        symbol = self.engine.params.symbol
        if not symbol:
            return
        payload = self._state_svc.load_reduction(db, symbol=symbol)
        if payload is None:
            return
        action = str(payload.get("action") or "").upper()
        cause = str(payload.get("cause") or "").upper()
        if action not in _POSITION_REDUCING_ACTIONS or cause not in {
            item.value for item in ReductionCause
        }:
            reason = (
                f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} invalid persisted "
                f"reduction metadata for {symbol}"
            )
            logger.critical(reason)
            started_at = payload.get("started_at")
            if not isinstance(started_at, datetime):
                started_at = datetime.now(timezone.utc)
            self._reduction_intents[symbol] = _ReductionIntent(
                action=action or "INVALID",
                cause=cause or "INVALID",
                reason=reason,
                trigger_price=float(payload.get("trigger_price") or 0),
                started_at=started_at,
            )
            self.risk.pause(reason, auto_resumable=False)
            record_trade_event(
                db,
                event_type="REDUCTION_RECOVERY_FAILED",
                symbol=symbol,
                status="ERROR",
                message=reason,
                payload={"action": action, "cause": cause},
            )
            db.commit()
            return
        started_at = payload.get("started_at")
        if not isinstance(started_at, datetime):
            started_at = datetime.now(timezone.utc)
        self._reduction_intents[symbol] = _ReductionIntent(
            action=action,
            cause=cause,
            reason=str(payload.get("reason") or cause),
            trigger_price=float(payload.get("trigger_price") or 0),
            started_at=started_at,
        )
        logger.warning("restored persisted reduction for %s: %s", symbol, cause)

    def _persist_reduction(self, intent: _ReductionIntent, symbol: str) -> bool:
        try:
            with self._db_session() as db:
                self._state_svc.persist_reduction(
                    db,
                    symbol=symbol,
                    action=intent.action,
                    cause=intent.cause,
                    reason=intent.reason,
                    started_at=intent.started_at,
                    trigger_price=intent.trigger_price,
                )
        except Exception:
            logger.critical("failed to persist reduction intent for %s", symbol, exc_info=True)
            return False
        with self._state_lock:
            self._reduction_intents[symbol] = intent
        try:
            with self._db_session() as db:
                record_trade_event(
                    db,
                    event_type="RISK_REDUCTION_TRIGGERED",
                    symbol=symbol,
                    side=intent.action,
                    status="REDUCING",
                    message=intent.reason,
                    payload={
                        "source": "deterministic_exit_policy",
                        "cause": intent.cause,
                        "trigger_price": intent.trigger_price,
                    },
                )
                db.commit()
        except Exception:
            logger.exception("failed to record reduction event for %s", symbol)
        return True

    def _clear_reduction(self, symbol: str, *, reason: str) -> bool:
        with self._state_lock:
            previous = self._reduction_intents.get(symbol)
        try:
            with self._db_session() as db:
                self._state_svc.clear_reduction(db, symbol=symbol)
        except Exception:
            logger.exception("failed to clear reduction state for %s", symbol)
            self.risk.pause(
                f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} failed to clear "
                f"persisted reduction state for {symbol}",
                auto_resumable=False,
            )
            return False
        with self._state_lock:
            if self._reduction_intents.get(symbol) is previous:
                self._reduction_intents.pop(symbol, None)
        if previous is not None:
            try:
                with self._db_session() as db:
                    record_trade_event(
                        db,
                        event_type="RISK_REDUCTION_COMPLETED",
                        symbol=symbol,
                        side=previous.action,
                        status="IDLE",
                        message=reason,
                        payload={"source": "deterministic_exit_policy", "cause": previous.cause},
                    )
                    db.commit()
            except Exception:
                logger.exception("failed to record reduction completion event for %s", symbol)
        return True

    def _complete_reduction(self, symbol: str, *, cause: str, reason: str) -> None:
        if not self._clear_reduction(symbol, reason="broker fill completed reduction"):
            return
        if cause == ReductionCause.DAILY_LOSS.value:
            self.risk.pause(reason, auto_resumable=False)
        elif cause in {ReductionCause.PRICE_STOP.value, ReductionCause.TIME_STOP.value}:
            self.risk.pause(reason, auto_resumable=True)
        with self._db_session() as db:
            self._state_svc.persist(db, self.engine, self.risk)
        self._set_last_action_message(
            f"{symbol} reduction FILLED; broker position is flat ({cause})"
        )
        self._broadcast_status()

    def execution_state(self) -> tuple[str, str, datetime | None]:
        with self._state_lock:
            intent = self._reduction_intents.get(self.engine.params.symbol)
            if intent is None:
                return "IDLE", "", None
            return "REDUCING", intent.reason, intent.started_at

    def _pause_if_unrealized_loss_limit_reached(self, symbol: str, last_price: float) -> bool:
        if last_price <= 0:
            return False
        with self._state_lock:
            if self.risk.paused or self.risk.kill_switch:
                return False
            max_daily_loss = Decimal(str(self.risk.config.max_daily_loss or 0))
            realized_pnl = Decimal(str(self.risk.daily_pnl))
        if max_daily_loss <= 0:
            return False

        tracked = self._trade_svc.tracked_position(symbol)
        if tracked is None:
            return False
        quantity = tracked.quantity
        cost = tracked.cost
        if quantity <= 0 or cost <= 0:
            return False
        avg_price = cost / quantity
        if tracked.side == "SHORT":
            unrealized_pnl = (avg_price - Decimal(str(last_price))) * quantity
        else:
            unrealized_pnl = (Decimal(str(last_price)) - avg_price) * quantity
        combined_pnl = realized_pnl + unrealized_pnl
        if combined_pnl > -max_daily_loss:
            return False

        reason = (
            "unrealized daily loss limit reached: "
            f"realized={float(realized_pnl):.2f}, "
            f"unrealized={float(unrealized_pnl):.2f}, "
            f"combined={float(combined_pnl):.2f}, "
            f"limit={float(max_daily_loss):.2f}"
        )
        self.risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record unrealized loss risk event")
        try:
            self.notifier.notify_risk_event("UNREALIZED_LOSS_LIMIT", reason)
        except Exception:
            logger.exception("failed to send unrealized loss risk notification")
        with self._db_session() as db:
            self._state_svc.persist(db, self.engine, self.risk)
        self._broadcast_status()
        return True

    def _risk_rejection_allows_action(self, action: str) -> bool:
        if action not in _POSITION_REDUCING_ACTIONS or self.risk.kill_switch:
            return False
        if self.risk.paused and self.risk.pause_reason.startswith(
            _OPERATIONAL_PAUSE_PREFIXES
        ):
            return self.risk.protective_exit_permitted
        return True

    def _should_evaluate_reducing_trigger(self, engine: StrategyEngine, price: float) -> bool:
        if self.risk.kill_switch or price <= 0:
            return False
        if engine.state == EngineState.LONG:
            return engine.params.sell_high > 0 and price >= engine.params.sell_high
        if engine.state == EngineState.SHORT:
            return engine.params.buy_low > 0 and price <= engine.params.buy_low
        return False

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

    @staticmethod
    def _credential_identity_fingerprint(credentials: dict[str, str]) -> str:
        identity_parts = (
            credentials.get("LONGPORT_APP_KEY", ""),
            credentials.get("LONGPORT_APP_SECRET", ""),
            credentials.get("LONGPORT_ACCESS_TOKEN", ""),
        )
        if not any(identity_parts):
            return ""
        material = "\0".join(identity_parts).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _apply_credentials(
        self,
        credentials: PlainCredentials,
        *,
        resubscribe: bool,
        validate_switch: bool = False,
    ) -> None:
        with self._state_lock:
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

            credential_env = {
                "LONGPORT_APP_KEY": (
                    credentials.longbridge_app_key or settings.longbridge_app_key
                ),
                "LONGPORT_APP_SECRET": (
                    credentials.longbridge_app_secret or settings.longbridge_app_secret
                ),
                "LONGPORT_ACCESS_TOKEN": (
                    credentials.longbridge_access_token
                    or settings.longbridge_access_token
                ),
            }
            broker_identity_fingerprint = self._credential_identity_fingerprint(
                credential_env
            )
            previous_env = {name: os.environ.get(name) for name in credential_env}
            new_broker: BrokerGateway | None = None
            try:
                for name, value in credential_env.items():
                    self._set_or_clear_env(name, value)
                new_broker = self._build_broker(self._audit)
                register = getattr(new_broker, "register_disconnect_hook", None)
                if callable(register):
                    register(self._on_disconnect)

                if validate_switch:
                    new_positions = new_broker.get_positions()
                    new_orders = new_broker.get_today_orders()
                    if any(
                        Decimal(str(position.quantity)) > 0
                        for position in new_positions
                    ):
                        raise CredentialSwitchBlockedError(
                            "new broker account already has positions"
                        )
                    if any(
                        str(order.status).upper() not in _TERMINAL_ORDER_STATUSES
                        for order in new_orders
                    ):
                        raise CredentialSwitchBlockedError(
                            "new broker account already has live orders"
                        )
            except Exception:
                for name, value in previous_env.items():
                    self._set_or_clear_env(name, value or "")
                if new_broker is not None:
                    new_broker.close()
                close_new_notifier = getattr(new_notifier, "close", None)
                if callable(close_new_notifier):
                    close_new_notifier()
                raise

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
            self._broker_identity_fingerprint = broker_identity_fingerprint
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

    def _record_order(
        self,
        order_id: str,
        symbol: str,
        side: str,
        qty: float,
        price: float,
        status: str = "SUBMITTED",
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
        ledger_metadata: dict[str, object] | None = None,
    ) -> None:
        normalized_status = str(status or "SUBMITTED").upper()
        effective_filled_at = filled_at
        if normalized_status == "FILLED" and effective_filled_at is None:
            effective_filled_at = datetime.now(timezone.utc)
        if normalized_status == "FILLED" and not executed_quantity:
            executed_quantity = qty
        if normalized_status == "FILLED" and not executed_price:
            executed_price = price
        submission_payload: dict[str, object] = {
            "quantity": qty,
            "price": price,
            "source": "runner",
        }
        metadata = ledger_metadata or {}
        submission_payload.update({
            key: value.isoformat() if isinstance(value, datetime) else value
            for key, value in metadata.items()
            if key not in {"config_snapshot"}
        })
        if self._broker_identity_fingerprint:
            submission_payload["broker_identity_fingerprint"] = (
                self._broker_identity_fingerprint
            )
        with self._order_persistence_lock:
            with self._db_session() as db:
                existing = None
                if order_id:
                    existing = (
                        db.query(OrderRecord)
                        .filter(OrderRecord.broker_order_id == order_id)
                        .first()
                    )
                if existing is not None:
                    self._apply_order_ledger_metadata(existing, metadata)
                    existing.symbol = symbol or existing.symbol
                    existing.side = side or existing.side
                    existing.quantity = qty or existing.quantity
                    existing.price = price or existing.price
                    if self._order_status_rank(normalized_status) > self._order_status_rank(
                        existing.status
                    ) or normalized_status == existing.status:
                        existing.status = normalized_status
                    if executed_quantity is not None:
                        previous_quantity = float(existing.executed_quantity or 0)
                        existing.executed_quantity = max(
                            previous_quantity,
                            float(executed_quantity),
                        )
                        if (
                            executed_price is not None
                            and executed_price > 0
                            and float(executed_quantity) >= previous_quantity
                        ):
                            existing.executed_price = float(executed_price)
                    if effective_filled_at is not None and existing.filled_at is None:
                        existing.filled_at = effective_filled_at
                    self._update_execution_outcome_fields(existing)
                    submitted_event_exists = (
                        db.query(TradeEvent.id)
                        .filter(
                            TradeEvent.event_type == "ORDER_SUBMITTED",
                            TradeEvent.broker_order_id == order_id,
                        )
                        .first()
                        is not None
                    )
                    if not submitted_event_exists:
                        record_trade_event(
                            db,
                            event_type="ORDER_SUBMITTED",
                            symbol=symbol,
                            broker_order_id=order_id,
                            side=side,
                            status=normalized_status,
                            message=f"{side} order submitted",
                            payload=submission_payload,
                        )
                    db.add(existing)
                    db.commit()
                    return

                order = OrderRecord(
                    broker_order_id=order_id,
                    symbol=symbol,
                    side=side,
                    quantity=qty,
                    price=price,
                    executed_quantity=executed_quantity,
                    executed_price=executed_price,
                    status=normalized_status,
                    filled_at=effective_filled_at,
                )
                self._apply_order_ledger_metadata(order, metadata)
                self._update_execution_outcome_fields(order)
                db.add(order)
                record_trade_event(
                    db,
                    event_type="ORDER_SUBMITTED",
                    symbol=symbol,
                    broker_order_id=order_id,
                    side=side,
                    status=normalized_status,
                    message=f"{side} order submitted",
                    payload=submission_payload,
                )
                if normalized_status in _TERMINAL_ORDER_STATUSES or normalized_status == "PARTIAL_FILLED":
                    record_trade_event(
                        db,
                        event_type=self._order_event_type_for_status(normalized_status),
                        symbol=symbol,
                        broker_order_id=order_id,
                        side=side,
                        status=normalized_status,
                        message=f"{side} order returned immediate status {normalized_status}",
                        payload={"quantity": qty, "price": price, "source": "runner_submit_result"},
                    )
                db.commit()

    @staticmethod
    def _order_status_rank(status: str) -> int:
        return {
            "SUBMITTED": 1,
            "PARTIAL_FILLED": 2,
            "REJECTED": 3,
            "CANCELLED": 3,
            "FILLED": 4,
        }.get(str(status).upper(), 0)

    def _update_order_status(
        self,
        order_id: str,
        status: str,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
        ledger_metadata: dict[str, object] | None = None,
    ) -> None:
        with self._order_persistence_lock:
            with self._db_session() as db:
                order = (
                    db.query(OrderRecord)
                    .filter(OrderRecord.broker_order_id == order_id)
                    .order_by(OrderRecord.id.desc())
                    .first()
                )
                if order is None:
                    raise OrderPersistenceError(
                        f"cannot update missing order {order_id} to status {status}"
                    )
                old_status = str(order.status or "").upper()
                incoming_status = str(status or "").upper()
                effective_status = (
                    incoming_status
                    if self._order_status_rank(incoming_status)
                    > self._order_status_rank(old_status)
                    or incoming_status == old_status
                    else old_status
                )
                old_executed_quantity = order.executed_quantity
                old_executed_price = order.executed_price
                old_filled_at = order.filled_at
                self._apply_order_ledger_metadata(order, ledger_metadata or {})
                order.status = effective_status
                normalized_executed_quantity = executed_quantity
                if (
                    effective_status == "FILLED"
                    and float(normalized_executed_quantity or 0) <= 0
                ):
                    normalized_executed_quantity = float(order.quantity)
                normalized_executed_price = executed_price
                if (
                    effective_status == "FILLED"
                    and float(normalized_executed_price or 0) <= 0
                ):
                    normalized_executed_price = float(
                        order.executed_price or order.price
                    )
                execution_advanced = float(normalized_executed_quantity or 0) > float(
                    old_executed_quantity or 0
                )
                execution_became_terminal = (
                    effective_status in _TERMINAL_ORDER_STATUSES
                    and old_status not in _TERMINAL_ORDER_STATUSES
                )
                if (
                    filled_at is not None
                    and (
                        order.filled_at is None
                        or execution_advanced
                        or execution_became_terminal
                    )
                    and (
                        effective_status == "FILLED"
                        or float(normalized_executed_quantity or 0) > 0
                    )
                ):
                    order.filled_at = filled_at
                if normalized_executed_quantity is not None:
                    order.executed_quantity = max(
                        float(order.executed_quantity or 0),
                        float(normalized_executed_quantity),
                    )
                if (
                    normalized_executed_price is not None
                    and normalized_executed_price > 0
                    and float(normalized_executed_quantity or 0)
                    >= float(old_executed_quantity or 0)
                ):
                    order.executed_price = normalized_executed_price
                self._update_execution_outcome_fields(order)
                changed = (
                    old_status != effective_status
                    or old_executed_quantity != order.executed_quantity
                    or old_executed_price != order.executed_price
                    or old_filled_at != order.filled_at
                )
                if changed:
                    record_trade_event(
                        db,
                        event_type=self._order_event_type_for_status(effective_status),
                        symbol=order.symbol,
                        broker_order_id=order_id,
                        side=order.side,
                        status=effective_status,
                        message=(
                            f"order status changed from {old_status} "
                            f"to {effective_status}"
                        ),
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

    @staticmethod
    def _apply_order_ledger_metadata(
        order: OrderRecord,
        metadata: dict[str, object],
    ) -> None:
        fields = {
            "decision_at",
            "decision_bid",
            "decision_ask",
            "decision_spread",
            "decision_spread_bps",
            "quote_age_ms",
            "config_version",
            "config_snapshot",
            "submit_started_at",
            "acknowledged_at",
            "broker_submitted_at",
            "broker_updated_at",
            "submit_latency_ms",
            "ack_latency_ms",
            "estimated_fee",
            "actual_fee",
            "fee_currency",
            "fee_source",
            "exit_cause",
            "exit_reason",
            "gross_pnl",
            "net_pnl",
            "pnl_source",
            "cost_basis_price",
            "cost_basis_quantity",
            "cost_basis_opened_at",
            "position_quantity_before",
            "pnl_fee",
            "pnl_fee_source",
            "pnl_fee_rate",
        }
        for name in fields:
            value = metadata.get(name)
            if value is not None:
                setattr(order, name, value)

    @staticmethod
    def _update_execution_outcome_fields(order: OrderRecord) -> None:
        if order.filled_at is not None and order.submit_started_at is not None:
            order.fill_latency_ms = max(
                0.0,
                (order.filled_at - order.submit_started_at).total_seconds() * 1000,
            )
        fill_price = float(order.executed_price or 0)
        fill_quantity = float(order.executed_quantity or 0)
        if fill_price <= 0 or fill_quantity <= 0:
            return
        side = str(order.side or "").upper()
        cost_basis_price = float(order.cost_basis_price or 0)
        position_quantity_before = float(order.position_quantity_before or 0)
        if (
            side in _POSITION_REDUCING_ACTIONS
            and cost_basis_price > 0
            and position_quantity_before >= fill_quantity
        ):
            fee_rate = max(0.0, float(order.pnl_fee_rate or 0))
            gross_pnl = (
                (fill_price - cost_basis_price) * fill_quantity
                if side == "SELL"
                else (cost_basis_price - fill_price) * fill_quantity
            )
            entry_fee = cost_basis_price * fill_quantity * fee_rate
            if order.actual_fee is not None:
                exit_fee = max(0.0, float(order.actual_fee))
                pnl_fee_source = "MIXED"
            else:
                exit_fee = fill_price * fill_quantity * fee_rate
                pnl_fee_source = "ESTIMATED"
            pnl_fee = entry_fee + exit_fee
            order.cost_basis_quantity = fill_quantity
            order.gross_pnl = gross_pnl
            order.pnl_fee = pnl_fee
            order.pnl_fee_source = pnl_fee_source
            order.net_pnl = gross_pnl - pnl_fee
        if side in {"BUY", "BUY_TO_COVER"}:
            reference = float(order.decision_ask or 0)
            price_cost = fill_price - reference
        else:
            reference = float(order.decision_bid or 0)
            price_cost = reference - fill_price
        if reference <= 0:
            return
        order.slippage_amount = price_cost * fill_quantity
        order.slippage_bps = price_cost / reference * 10_000

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
        entries: dict[
            str,
            tuple[Decimal, Decimal, str, datetime | None],
        ] = {}
        for row in rows:
            try:
                quantity = Decimal(str(row.quantity))
                cost = Decimal(str(row.cost))
            except Exception:
                continue
            if quantity > 0 and cost > 0:
                entries[row.symbol] = (
                    quantity,
                    cost,
                    str(getattr(row, "side", "") or "").upper(),
                    getattr(row, "opened_at", None) or getattr(row, "updated_at", None),
                )
        self._trade_svc.load_tracked_entries(entries)
        if entries:
            logger.info("restored %d tracked entry positions from db", len(entries))

    def _load_pending_orders(self, db: Session) -> list[str]:
        try:
            rows = (
                db.query(OrderRecord)
                .filter(OrderRecord.status.in_(_LIVE_ORDER_STATUSES))
                .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
                .all()
            )
        except Exception:
            logger.exception("failed to load pending orders")
            return ["db live orders could not be loaded into pending state"]

        now = time.monotonic()
        current_utc = datetime.now(timezone.utc)
        pending_orders: list[_PendingOrder] = []
        representation_issues: list[str] = []
        row_ids = {
            str(row.broker_order_id)
            for row in rows
            if str(row.broker_order_id or "")
        }
        submitted_events: list[TradeEvent] = []
        if row_ids:
            try:
                submitted_events = (
                    db.query(TradeEvent)
                    .filter(
                        TradeEvent.event_type == "ORDER_SUBMITTED",
                        TradeEvent.broker_order_id.in_(sorted(row_ids)),
                    )
                    .all()
                )
            except Exception:
                logger.exception(
                    "failed to load submission provenance for pending orders"
                )
        for row in rows:
            issue = self._live_order_representation_issue(
                row,
                source="db live order",
            )
            if issue is not None:
                representation_issues.append(issue)
                continue
            if not any(
                self._submission_event_matches_order(event, row)
                for event in submitted_events
                if str(event.broker_order_id or "")
                == str(row.broker_order_id or "")
            ):
                representation_issues.append(
                    "db live order "
                    f"id={row.broker_order_id or '<missing>'} lacks local "
                    "submission provenance"
                )
                continue
            try:
                quantity = Decimal(str(row.quantity))
                price = Decimal(str(row.price))
            except Exception:
                representation_issues.append(
                    f"db live order id={row.broker_order_id or '<missing>'}: "
                    "quantity or price cannot be represented"
                )
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
                    avg_price=(
                        Decimal(str(getattr(row, "cost_basis_price", None)))
                        if getattr(row, "cost_basis_price", None) is not None
                        else None
                    ),
                    pnl_fee_rate=(
                        Decimal(str(getattr(row, "pnl_fee_rate", None)))
                        if getattr(row, "pnl_fee_rate", None) is not None
                        else self._live_fee_rate_for_market(
                            "HK" if str(row.symbol).upper().endswith(".HK") else "US"
                        )
                    ),
                    next_status_check_at=0.0,
                    submitted_at=now - submitted_age_seconds,
                )
            )
        self._trade_svc.load_pending_orders(pending_orders)
        if pending_orders:
            logger.info("restored %d live pending orders from db", len(pending_orders))
        return representation_issues

    def _persist_tracked_entry(self, symbol: str, quantity: Decimal, cost: Decimal) -> None:
        with self._db_session() as db:
            existing = db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).first()
            if quantity <= 0:
                if existing is not None:
                    db.delete(existing)
                    db.commit()
                return
            if existing is None:
                runtime = self._runtime_for_symbol(symbol)
                engine_state = runtime[2].state if runtime is not None else self.engine.state
                existing = TrackedEntry(
                    symbol=symbol,
                    side="SHORT" if engine_state == EngineState.SHORT else "LONG",
                    quantity=float(quantity),
                    cost=float(cost),
                    opened_at=datetime.now(timezone.utc),
                )
                db.add(existing)
            else:
                existing.quantity = float(quantity)
                existing.cost = float(cost)
                existing.updated_at = datetime.now(timezone.utc)
            db.commit()

    def _latch_durable_fill_reconciliation_failure(
        self,
        db: Session,
        symbols: set[str],
        *,
        source: str,
        error: Exception,
    ) -> list[tuple[str, _ReductionIntent]]:
        unsafe_symbols = set(symbols)
        if self.engine.params.symbol:
            unsafe_symbols.add(self.engine.params.symbol)
        with self._state_lock:
            self._unsettled_position_symbols.update(unsafe_symbols)
        reason = (
            f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} recent durable fills "
            "could not be proved before position reconciliation"
        )
        self.risk.pause(reason, auto_resumable=False)
        logger.critical("%s: %s", reason, error)
        try:
            record_trade_event(
                db,
                event_type="DURABLE_FILL_RECONCILIATION_FAILED",
                status="ERROR",
                message=reason,
                payload={
                    "source": source,
                    "symbols": sorted(unsafe_symbols),
                    "error": str(error),
                },
            )
            self._state_svc.persist(db, self.engine, self.risk)
            db.commit()
        except Exception:
            logger.exception("failed to persist durable-fill reconciliation pause")
            try:
                db.rollback()
            except Exception:
                pass
        return []

    def _reconcile_tracked_entries_with_broker(
        self,
        db: Session,
        *,
        source: str = "startup_tracked_entry_reconcile",
        position_snapshot: list[Position] | None = None,
    ) -> list[tuple[str, _ReductionIntent]]:
        """Repair local cost-basis state from broker truth during startup.

        Position reconciliation is intentionally idempotent. It never replays a
        fill into risk accounting; the order ledger owns that. Instead it makes
        the persisted/in-memory tracked position match the broker's current
        quantity, preserving the local average price for reductions and using
        the broker/order fill price only when a position grew or was missing.
        """
        snapshot = self._trade_svc.snapshot_tracked_entries()
        reconciliation_symbols = set(snapshot)
        reconciliation_symbols.update(self._symbol_runtimes)
        if self.engine.params.symbol:
            reconciliation_symbols.add(self.engine.params.symbol)
        try:
            latest_fills = self._latest_filled_orders_by_symbol(
                db,
                reconciliation_symbols,
            )
        except DurableFillReconciliationError as exc:
            return self._latch_durable_fill_reconciliation_failure(
                db,
                reconciliation_symbols,
                source=source,
                error=exc,
            )
        try:
            positions = (
                position_snapshot
                if position_snapshot is not None
                else self.broker.get_positions()
            )
        except Exception as exc:
            logger.warning("tracked entry reconciliation skipped: %s", exc)
            unsafe_symbols: list[str] = []
            for symbol in snapshot:
                tracked = self._trade_svc.tracked_position(symbol)
                if tracked is not None and (
                    tracked.side not in {"LONG", "SHORT"} or tracked.opened_at is None
                ):
                    unsafe_symbols.append(symbol)
            primary_symbol = self.engine.params.symbol
            if primary_symbol:
                unsafe_symbols.append(primary_symbol)
            unsafe_symbols.extend(self._reduction_intents)
            unsafe_symbols.extend(latest_fills)
            if unsafe_symbols:
                with self._state_lock:
                    self._unsettled_position_symbols.update(unsafe_symbols)
            if unsafe_symbols:
                reason = (
                    f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                    "broker position reconciliation failed with unprotected local state: "
                    + ", ".join(sorted(set(unsafe_symbols)))
                )
                self.risk.revoke_protective_exits()
                if not (
                    self.risk.paused
                    and self.risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES)
                ):
                    self.risk.pause(reason, auto_resumable=False)
                record_trade_event(
                    db,
                    event_type="TRACKED_ENTRY_RECOVERY_FAILED",
                    status="ERROR",
                    message=reason,
                    payload={
                        "source": source,
                        "symbols": sorted(set(unsafe_symbols)),
                    },
                )
                db.commit()
            return []

        broker_positions: dict[str, list[tuple[str, Decimal, Decimal]]] = {}
        for pos in positions:
            side = str(pos.side).upper()
            if side not in {"LONG", "SHORT"}:
                continue
            try:
                qty = abs(Decimal(str(pos.quantity)))
            except Exception:
                continue
            if qty <= 0:
                continue
            try:
                avg_price = Decimal(str(pos.avg_price))
            except Exception:
                avg_price = Decimal("0")
            broker_positions.setdefault(pos.symbol, []).append((side, qty, avg_price))

        broker_only_symbols = set(broker_positions) - reconciliation_symbols
        reconciliation_symbols.update(broker_positions)
        if broker_only_symbols:
            try:
                latest_fills.update(
                    self._latest_filled_orders_by_symbol(db, broker_only_symbols)
                )
            except DurableFillReconciliationError as exc:
                return self._latch_durable_fill_reconciliation_failure(
                    db,
                    reconciliation_symbols,
                    source=source,
                    error=exc,
                )

        unsettled_symbols: set[str] = set()
        confirmed_expectation_symbols: set[str] = set()
        confirmed_flat_symbols: set[str] = set()
        with self._state_lock:
            expectation_symbols = set(self._post_fill_expectations)
        for symbol in sorted(reconciliation_symbols | expectation_symbols):
            tracked = self._trade_svc.tracked_position(symbol)
            broker_rows = broker_positions.get(symbol, [])
            broker_qty = sum((qty for _, qty, _ in broker_rows), Decimal("0"))
            with self._state_lock:
                expectation = self._post_fill_expectations.get(symbol)

            latest_fill_info = latest_fills.get(symbol)
            ambiguous_recent_fill = False
            if latest_fill_info is not None:
                latest_fill, semantic_action = latest_fill_info
                latest_action = str(latest_fill.side).upper()
                if semantic_action and latest_action in (
                    _ENTRY_ACTIONS | _POSITION_REDUCING_ACTIONS
                ):
                    durable_expectation = self._expectation_from_durable_fill(
                        db,
                        latest_fill,
                    )
                    if durable_expectation is None:
                        ambiguous_recent_fill = True
                    elif expectation is None:
                        expectation = durable_expectation
                else:
                    # Raw broker sides do not reveal open-vs-close intent.  A
                    # recent broker-discovered BUY may be a cover and SELL may
                    # be a short entry.  Unknown/new side enums are equally
                    # unsafe, so every unproven recent execution fails closed.
                    ambiguous_recent_fill = True

            if expectation is not None:
                with self._state_lock:
                    self._post_fill_expectations[symbol] = expectation
                runtime = self._runtime_for_symbol(symbol)
                if runtime is not None:
                    runtime[2].sync_state(
                        expectation.quantity > 0 and expectation.side == "LONG",
                        expectation.quantity > 0 and expectation.side == "SHORT",
                    )
            expectation_matches = (
                expectation is not None
                and not ambiguous_recent_fill
                and self._position_snapshot_matches_expectation(
                    symbol,
                    positions,
                    expectation,
                )
            )
            if expectation_matches and expectation is not None:
                self._apply_confirmed_position_expectation(
                    db,
                    symbol,
                    expectation,
                )
                with self._state_lock:
                    if self._post_fill_expectations.get(symbol) is expectation:
                        self._post_fill_expectations.pop(symbol, None)
                confirmed_expectation_symbols.add(symbol)
                if expectation.quantity <= 0:
                    confirmed_flat_symbols.add(symbol)
                continue

            recent_entry_unsettled = False
            if (
                expectation is None
                and tracked is not None
                and broker_qty <= 0
                and self._tracked_entry_is_in_settlement_grace(tracked.opened_at)
            ):
                recent_entry_unsettled = True

            if (
                expectation is None
                and not recent_entry_unsettled
                and not ambiguous_recent_fill
            ):
                continue

            unsettled_symbols.add(symbol)
            reason = (
                f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                f"broker position has not settled after fill for {symbol}"
            )
            self.risk.revoke_protective_exits()
            already_latched = self.risk.paused and self.risk.pause_reason == reason
            if not (
                self.risk.paused
                and self.risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES)
            ):
                self.risk.pause(reason, auto_resumable=False)
            if not already_latched:
                record_trade_event(
                    db,
                    event_type="POSITION_SETTLEMENT_PENDING",
                    symbol=symbol,
                    status="ERROR",
                    message=reason,
                    payload={"source": source},
                )

        primary_symbol = self.engine.params.symbol
        managed_symbols = set(snapshot)
        managed_symbols.update(self._symbol_runtimes)
        managed_symbols.update(broker_positions)
        if primary_symbol:
            managed_symbols.add(primary_symbol)

        unexpected_exposure = sorted(set(broker_positions) - {primary_symbol})
        if unexpected_exposure:
            reason = (
                f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                "broker exposure exists outside the primary strategy: "
                + ", ".join(unexpected_exposure)
            )
            self.risk.revoke_protective_exits()
            if not (
                self.risk.paused
                and self.risk.pause_reason.startswith(
                    _ORDER_RECONCILIATION_UNCERTAIN_PREFIX
                )
            ):
                self.risk.pause(reason, auto_resumable=False)
            record_trade_event(
                db,
                event_type="UNMANAGED_BROKER_EXPOSURE",
                status="ERROR",
                message=reason,
                payload={"source": source, "symbols": unexpected_exposure},
            )

        completed_reductions: list[tuple[str, _ReductionIntent]] = []
        avg_drift_keys_to_confirm: dict[str, tuple[object, ...]] = {}
        avg_drift_keys_to_clear: set[str] = set()
        for symbol in sorted(confirmed_flat_symbols):
            intent = self._reduction_intents.get(symbol)
            if intent is not None and self._trade_svc.pending_order_for(symbol) is None:
                completed_reductions.append((symbol, intent))
        for symbol in sorted(managed_symbols):
            if symbol in unsettled_symbols or symbol in confirmed_expectation_symbols:
                continue
            if self._trade_svc.pending_order_for(symbol) is not None:
                # Broker position already includes any live partial execution,
                # while pending finalization consumes the broker's cumulative
                # executed quantity. Keeping the pre-submit tracked inventory
                # here prevents that cumulative fill from being applied twice.
                continue
            tracked = self._trade_svc.tracked_position(symbol)
            tracked_qty = tracked.quantity if tracked is not None else Decimal("0")
            tracked_cost = tracked.cost if tracked is not None else Decimal("0")
            tracked_side = tracked.side if tracked is not None else ""
            broker_rows = broker_positions.get(symbol, [])
            sides = {side for side, _, _ in broker_rows}
            if len(sides) > 1:
                reason = (
                    f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                    f"both long and short broker positions found for {symbol}"
                )
                logger.error(reason)
                self.risk.pause(reason, auto_resumable=False)
                record_trade_event(
                    db,
                    event_type="TRACKED_ENTRY_RECOVERY_FAILED",
                    symbol=symbol,
                    status="ERROR",
                    message=reason,
                    payload={"source": source},
                )
                continue

            broker_qty = sum((qty for _, qty, _ in broker_rows), Decimal("0"))
            broker_side = next(iter(sides), "")
            broker_cost = sum(
                (qty * avg for _, qty, avg in broker_rows if avg > 0),
                Decimal("0"),
            )
            all_broker_prices_known = bool(broker_rows) and all(avg > 0 for _, _, avg in broker_rows)
            broker_avg = broker_cost / broker_qty if broker_qty > 0 and all_broker_prices_known else Decimal("0")
            row = db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).first()

            quantity_grew_without_cost = bool(
                tracked is not None
                and tracked_side == broker_side
                and broker_qty > tracked_qty
                and broker_avg <= 0
            )
            if quantity_grew_without_cost and tracked is not None:
                reason = (
                    f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                    f"broker quantity grew from {tracked_qty} to {broker_qty} for "
                    f"{symbol}, but the added position cost is unavailable; "
                    "preserving the last durable tracked entry"
                )
                logger.critical(reason)
                self.risk.pause(reason, auto_resumable=False)
                if row is None:
                    row = TrackedEntry(symbol=symbol)
                    db.add(row)
                    row.side = tracked_side
                    row.quantity = float(tracked_qty)
                    row.cost = float(tracked_cost)
                    row.opened_at = tracked.opened_at
                    row.updated_at = datetime.now(timezone.utc)
                record_trade_event(
                    db,
                    event_type="TRACKED_ENTRY_RECOVERY_FAILED",
                    symbol=symbol,
                    status="ERROR",
                    message=reason,
                    payload={
                        "source": source,
                        "tracked_quantity": float(tracked_qty),
                        "tracked_avg_price": float(tracked.avg_price),
                        "broker_quantity": float(broker_qty),
                        "broker_side": broker_side,
                        "broker_avg_price": 0.0,
                        "preserved": True,
                    },
                )
                continue

            quantity_drift = tracked_qty != broker_qty
            side_drift = bool(broker_side and tracked_side and broker_side != tracked_side)
            avg_price_drift = bool(
                tracked is not None
                and broker_avg > 0
                and tracked.avg_price > 0
                and abs(tracked.avg_price - broker_avg) / broker_avg > Decimal("0.000001")
            )
            if tracked_qty > 0:
                drift_pct = abs(tracked_qty - broker_qty) / tracked_qty
            else:
                drift_pct = Decimal("1") if broker_qty > 0 else Decimal("0")
            should_record_drift = quantity_drift and (
                drift_pct >= _POSITION_DRIFT_PCT_TOLERANCE
                or abs(tracked_qty - broker_qty) >= _POSITION_DRIFT_SHARE_TOLERANCE
            )
            avg_drift_key = (
                tracked_side,
                tracked_qty.quantize(_POSITION_DRIFT_SIGNATURE_QUANTUM),
                (
                    tracked.avg_price if tracked is not None else Decimal("0")
                ).quantize(_POSITION_DRIFT_SIGNATURE_QUANTUM),
                broker_side,
                broker_qty.quantize(_POSITION_DRIFT_SIGNATURE_QUANTUM),
                broker_avg.quantize(_POSITION_DRIFT_SIGNATURE_QUANTUM),
            )
            should_record_avg_drift = bool(
                avg_price_drift
                and self._tracked_avg_drift_warning_keys.get(symbol)
                != avg_drift_key
            )
            if avg_price_drift:
                avg_drift_keys_to_confirm[symbol] = avg_drift_key
                avg_drift_keys_to_clear.discard(symbol)
            else:
                avg_drift_keys_to_confirm.pop(symbol, None)
                avg_drift_keys_to_clear.add(symbol)
            tracked_avg_is_durable = False

            if broker_qty <= 0:
                if row is not None:
                    db.delete(row)
                intent = self._reduction_intents.get(symbol)
                if intent is not None and self._trade_svc.pending_order_for(symbol) is None:
                    completed_reductions.append((symbol, intent))
            else:
                recovery_price = Decimal("0")
                recovered_opened_at: datetime | None = None
                if tracked is None or tracked_side != broker_side or broker_avg <= 0:
                    recovery_price, recovered_opened_at = self._recovery_entry_fill(
                        db,
                        symbol,
                        broker_side,
                    )
                runtime = self._runtime_for_symbol(symbol)
                recovery_engine = runtime[2] if runtime is not None else self.engine
                tracked_avg_is_durable = bool(
                    tracked is not None
                    and tracked_side == broker_side
                    and tracked.avg_price > 0
                    and broker_qty <= tracked_qty
                )
                if tracked_avg_is_durable and tracked is not None:
                    # Broker average prices can lag a just-closed and reopened
                    # position. When direction matches and broker inventory did
                    # not grow, the fill-derived durable cost is authoritative.
                    desired_avg = tracked.avg_price
                elif broker_avg > 0:
                    desired_avg = broker_avg
                elif tracked is not None and tracked_side == broker_side and tracked.avg_price > 0:
                    desired_avg = tracked.avg_price
                else:
                    desired_avg = recovery_price

                if desired_avg <= 0:
                    reason = (
                        f"{_POSITION_RECONCILIATION_UNCERTAIN_PREFIX} "
                        f"cannot recover tracked entry cost for broker position {symbol}"
                    )
                    logger.critical(reason)
                    self.risk.pause(reason, auto_resumable=False)
                    record_trade_event(
                        db,
                        event_type="TRACKED_ENTRY_RECOVERY_FAILED",
                        symbol=symbol,
                        status="ERROR",
                        message=reason,
                        payload={
                            "source": source,
                            "broker_quantity": float(broker_qty),
                            "broker_side": broker_side,
                        },
                    )
                    continue

                opened_at = (
                    tracked.opened_at
                    if tracked is not None and tracked_side == broker_side and tracked.opened_at is not None
                    else (
                        recovered_opened_at
                        or datetime.now(timezone.utc)
                        - timedelta(minutes=max(1, recovery_engine.params.max_holding_minutes))
                    )
                )
                desired_cost = desired_avg * broker_qty
                if row is None:
                    row = TrackedEntry(symbol=symbol)
                    db.add(row)
                row.side = broker_side
                row.quantity = float(broker_qty)
                row.cost = float(desired_cost)
                row.opened_at = opened_at
                row.updated_at = datetime.now(timezone.utc)

            if should_record_drift or side_drift or should_record_avg_drift:
                if should_record_drift:
                    drift_message = (
                        f"tracked qty {tracked_qty} diverged from broker qty {broker_qty} for {symbol}"
                    )
                elif side_drift:
                    drift_message = (
                        f"tracked side {tracked_side} diverged from broker side {broker_side} for {symbol}"
                    )
                else:
                    drift_message = (
                        f"tracked avg price diverged from broker avg price {broker_avg} for {symbol}"
                    )
                payload = {
                    "symbol": symbol,
                    "tracked_quantity": float(tracked_qty),
                    "tracked_avg_price": float(tracked_cost / tracked_qty) if tracked_qty > 0 else 0.0,
                    "tracked_side": tracked_side,
                    "broker_quantity": float(broker_qty),
                    "broker_avg_price": float(broker_avg),
                    "broker_sides": sorted(sides),
                    "drift_pct": float(drift_pct),
                    "source": source,
                    "repaired": bool(
                        quantity_drift
                        or side_drift
                        or (avg_price_drift and not tracked_avg_is_durable)
                    ),
                    "preserved": tracked_avg_is_durable,
                    "cost_authority": (
                        "DURABLE_TRACKED_ENTRY"
                        if tracked_avg_is_durable
                        else "BROKER_POSITION"
                        if broker_qty > 0
                        else "NO_POSITION"
                    ),
                }
                record_trade_event(
                    db,
                    event_type="TRACKED_ENTRY_DRIFT",
                    symbol=symbol,
                    status="WARNING",
                    message=drift_message,
                    payload=payload,
                )
        with self._state_lock:
            self._unsettled_position_symbols = set(unsettled_symbols)
        db.commit()
        for symbol in avg_drift_keys_to_clear:
            self._tracked_avg_drift_warning_keys.pop(symbol, None)
        self._tracked_avg_drift_warning_keys.update(avg_drift_keys_to_confirm)
        self._load_tracked_entries(db)
        return completed_reductions

    def _latest_filled_orders_by_symbol(
        self,
        db: Session,
        symbols: set[str],
    ) -> dict[str, tuple[OrderRecord, bool]]:
        """Return recent fills and whether their action came from this service."""
        if not symbols:
            return {}
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=_POST_FILL_SETTLEMENT_GRACE_SECONDS
        )
        try:
            recent_terminal_events = (
                db.query(TradeEvent.broker_order_id, TradeEvent.created_at)
                .filter(
                    TradeEvent.event_type.in_(
                        {"ORDER_FILLED", "ORDER_CANCELLED", "ORDER_REJECTED"}
                    ),
                    TradeEvent.created_at >= cutoff,
                    TradeEvent.broker_order_id != "",
                )
                .all()
            )
            terminal_observed_at: dict[str, datetime] = {}
            for order_id, observed_at in recent_terminal_events:
                normalized_order_id = str(order_id or "")
                if not normalized_order_id or observed_at is None:
                    continue
                normalized_observed_at = AppRunner._as_utc(observed_at)
                current = terminal_observed_at.get(normalized_order_id)
                if current is None or normalized_observed_at > current:
                    terminal_observed_at[normalized_order_id] = normalized_observed_at

            recency_clause = (
                (
                    OrderRecord.filled_at.is_not(None)
                    & (OrderRecord.filled_at >= cutoff)
                )
                | (
                    OrderRecord.filled_at.is_(None)
                    & (OrderRecord.created_at >= cutoff)
                )
            )
            if terminal_observed_at:
                recency_clause = recency_clause | OrderRecord.broker_order_id.in_(
                    sorted(terminal_observed_at)
                )
            rows = (
                db.query(OrderRecord)
                .filter(
                    OrderRecord.symbol.in_(sorted(symbols)),
                    (
                        (OrderRecord.status == "FILLED")
                        | (
                            OrderRecord.status.in_({"CANCELLED", "REJECTED"})
                            & (OrderRecord.executed_quantity > 0)
                        )
                    ),
                    recency_clause,
                )
                .all()
            )
        except Exception as exc:
            logger.exception("failed to load durable fills for position reconciliation")
            raise DurableFillReconciliationError(
                "recent durable fills could not be loaded"
            ) from exc

        eligible_rows: list[OrderRecord] = []
        for row in rows:
            row_status = str(getattr(row, "status", "") or "").upper()
            try:
                row_executed_quantity = Decimal(
                    str(getattr(row, "executed_quantity", 0) or 0)
                )
            except Exception:
                row_executed_quantity = Decimal("0")
            if row_status != "FILLED" and not (
                row_status in {"CANCELLED", "REJECTED"}
                and row_executed_quantity > 0
            ):
                continue
            order_id = str(getattr(row, "broker_order_id", "") or "")
            timestamps = [
                AppRunner._as_utc(timestamp)
                for timestamp in (
                    getattr(row, "filled_at", None),
                    getattr(row, "created_at", None),
                    terminal_observed_at.get(order_id),
                )
                if timestamp is not None
            ]
            timestamp = max(timestamps, default=None)
            if timestamp is None or AppRunner._as_utc(timestamp) < cutoff:
                continue
            eligible_rows.append(row)
        rows = eligible_rows

        order_ids = {
            str(row.broker_order_id)
            for row in rows
            if str(row.broker_order_id or "")
        }
        submitted_order_ids: set[str] = set()
        if order_ids:
            try:
                rows_by_order_id = {
                    str(row.broker_order_id): row
                    for row in rows
                    if str(row.broker_order_id or "")
                }
                submitted_order_ids = {
                    str(event.broker_order_id)
                    for event in (
                        db.query(TradeEvent)
                        .filter(
                            TradeEvent.event_type == "ORDER_SUBMITTED",
                            TradeEvent.broker_order_id.in_(sorted(order_ids)),
                        )
                        .all()
                    )
                    if self._submission_event_matches_order(
                        event,
                        rows_by_order_id.get(str(event.broker_order_id)),
                    )
                }
            except Exception:
                # Losing provenance must make the fill ambiguous, never turn a
                # raw broker BUY/SELL into an inferred open/close action.
                logger.exception("failed to prove semantic action for recent fills")

        latest: dict[str, tuple[datetime, int, OrderRecord, bool]] = {}
        for row in rows:
            order_id = str(row.broker_order_id or "")
            observed_at = max(
                (
                    AppRunner._as_utc(timestamp)
                    for timestamp in (
                        row.filled_at,
                        row.created_at,
                        terminal_observed_at.get(order_id),
                    )
                    if timestamp is not None
                ),
                default=cutoff,
            )
            candidate_key = (observed_at, int(row.id or 0))
            current = latest.get(row.symbol)
            if current is None or candidate_key > (current[0], current[1]):
                latest[row.symbol] = (
                    candidate_key[0],
                    candidate_key[1],
                    row,
                    str(row.broker_order_id or "") in submitted_order_ids,
                )
        return {
            symbol: (candidate[2], candidate[3])
            for symbol, candidate in latest.items()
        }

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def _expectation_from_durable_fill(
        self,
        db: Session,
        order: OrderRecord,
    ) -> _PostFillExpectation | None:
        """Rebuild the exact post-fill position across the persistence crash gap."""
        action = str(order.side or "").upper()
        if action not in (_ENTRY_ACTIONS | _POSITION_REDUCING_ACTIONS):
            return None
        fill_timestamp = order.filled_at or order.created_at
        try:
            fill_quantity = Decimal(str(order.executed_quantity))
        except Exception:
            return None
        if not fill_quantity.is_finite() or fill_quantity <= 0:
            return None

        row = (
            db.query(TrackedEntry)
            .filter(TrackedEntry.symbol == order.symbol)
            .first()
        )
        expected_side = "SHORT" if action in {"SELL_SHORT", "BUY_TO_COVER"} else "LONG"
        row_quantity = Decimal("0")
        row_cost = Decimal("0")
        row_side = expected_side
        row_opened_at: datetime | None = None
        fill_already_applied = False
        if row is not None:
            try:
                row_quantity = Decimal(str(row.quantity))
                row_cost = Decimal(str(row.cost))
            except Exception:
                return None
            row_side = str(row.side or "").upper()
            if (
                not row_quantity.is_finite()
                or not row_cost.is_finite()
                or row_quantity <= 0
                or row_cost <= 0
                or row_side != expected_side
            ):
                return None
            row_opened_at = row.opened_at
            if row.updated_at is not None:
                fill_already_applied = self._as_utc(row.updated_at) >= self._as_utc(
                    fill_timestamp
                )

        if fill_already_applied:
            desired_quantity = row_quantity
            desired_cost = row_cost
            desired_opened_at = row_opened_at
        elif action in _ENTRY_ACTIONS:
            try:
                fill_price = Decimal(str(order.executed_price or order.price))
            except Exception:
                return None
            if not fill_price.is_finite() or fill_price <= 0:
                return None
            desired_quantity = row_quantity + fill_quantity
            desired_cost = row_cost + fill_quantity * fill_price
            desired_opened_at = row_opened_at or self._as_utc(fill_timestamp)
        elif row is None:
            # A missing row after a reducing fill is the durable result of a
            # completed full close.  Partial fills retain a tracked row.
            desired_quantity = Decimal("0")
            desired_cost = Decimal("0")
            desired_opened_at = None
        else:
            desired_quantity = max(Decimal("0"), row_quantity - fill_quantity)
            average_cost = row_cost / row_quantity
            desired_cost = average_cost * desired_quantity
            desired_opened_at = row_opened_at

        if desired_quantity <= 0:
            expected_side = ""
            desired_quantity = Decimal("0")
            desired_cost = Decimal("0")
            desired_opened_at = None
        elif not desired_cost.is_finite() or desired_cost <= 0:
            return None

        return _PostFillExpectation(
            side=expected_side,
            quantity=desired_quantity,
            recorded_at=time.monotonic(),
            cost=desired_cost,
            opened_at=desired_opened_at,
        )

    @staticmethod
    def _apply_confirmed_position_expectation(
        db: Session,
        symbol: str,
        expectation: _PostFillExpectation,
    ) -> None:
        row = db.query(TrackedEntry).filter(TrackedEntry.symbol == symbol).first()
        if expectation.quantity <= 0:
            if row is not None:
                db.delete(row)
            return
        if (
            expectation.side not in {"LONG", "SHORT"}
            or expectation.cost is None
            or not expectation.cost.is_finite()
            or expectation.cost <= 0
        ):
            raise ValueError(f"invalid confirmed position expectation for {symbol}")
        if row is None:
            row = TrackedEntry(symbol=symbol)
            db.add(row)
        row.side = expectation.side
        row.quantity = float(expectation.quantity)
        row.cost = float(expectation.cost)
        row.opened_at = expectation.opened_at or datetime.now(timezone.utc)
        row.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _recovery_entry_fill(
        db: Session,
        symbol: str,
        side: str,
    ) -> tuple[Decimal, datetime | None]:
        entry_action = "SELL_SHORT" if side == "SHORT" else "BUY"
        try:
            order = (
                db.query(OrderRecord)
                .filter(
                    OrderRecord.symbol == symbol,
                    OrderRecord.side == entry_action,
                    OrderRecord.status == "FILLED",
                )
                .order_by(OrderRecord.filled_at.desc(), OrderRecord.created_at.desc())
                .first()
            )
        except Exception:
            logger.exception("failed to load recovery entry fill for %s", symbol)
            return Decimal("0"), None
        if order is None:
            return Decimal("0"), None
        try:
            price = Decimal(str(order.executed_price or order.price or 0))
        except Exception:
            price = Decimal("0")
        return price, order.filled_at or order.created_at

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
        normalized = str(action or "").upper()
        if normalized in {"BUY", "BUY_TO_COVER"}:
            return "BUY"
        if normalized in {"SELL", "SELL_SHORT"}:
            return "SELL"
        return ""

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
                    allow_position_addons=False,
                    stop_loss_pct=primary_params.stop_loss_pct,
                    max_holding_minutes=primary_params.max_holding_minutes,
                    entry_cutoff_minutes_before_close=primary_params.entry_cutoff_minutes_before_close,
                    flatten_minutes_before_close=primary_params.flatten_minutes_before_close,
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
                        runtime_params.allow_position_addons = False
                        runtime_params.stop_loss_pct = primary_params.stop_loss_pct
                        runtime_params.max_holding_minutes = primary_params.max_holding_minutes
                        runtime_params.entry_cutoff_minutes_before_close = primary_params.entry_cutoff_minutes_before_close
                        runtime_params.flatten_minutes_before_close = primary_params.flatten_minutes_before_close
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

    def _remember_symbol_runtime_quote(
        self,
        quote: Quote,
        observed_at: datetime,
        *,
        trusted: bool = True,
    ) -> None:
        with self._state_lock:
            runtime = self._symbol_runtimes.get(quote.symbol)
            if runtime is None:
                logger.warning("ignoring quote for unknown symbol %s", quote.symbol)
                return
            if trusted:
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
        target_symbol = symbol or self.engine.params.symbol
        if not is_trading_hours(target_market):
            reason = f"non-RTH for {target_market}"
        elif action in _ENTRY_ACTIONS and is_opening_warmup(target_market, settings.trading_open_warmup_minutes):
            reason = f"opening warmup for {target_market}"
        else:
            return None
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
