from __future__ import annotations

import logging
import inspect
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from threading import RLock
from typing import TYPE_CHECKING, Callable, Final, Optional, Protocol, assert_never, cast

from app.config import settings
from app.core.fees import (
    estimate_round_trip_fee,
    evaluate_long_round_trip_edge,
    evaluate_long_round_trip_reward_risk,
)
from app.core.holiday_calendar import is_coverage_expired
from app.core.log_throttle import RepeatedLogThrottle
from app.core.market_calendar import (
    is_closing_window,
    is_opening_warmup,
    is_trading_hours,
    market_for_symbol,
    trade_day_for,
)
from app.core.risk import TradingState

if TYPE_CHECKING:
    from app.core.audit import AuditLogger
    from app.core.broker import BrokerGateway, OrderResult, Quote
    from app.core.engine import EngineSnapshot, EngineState
    from app.core.notifiers import NotifierInterface
    from app.core.risk import RiskController

logger = logging.getLogger("auto_trade.services.trade_execution_service")

_REDUCTION_AVAILABILITY_LOG_WINDOW_SECONDS = 3600.0
_REDUCTION_AVAILABILITY_LOG_THROTTLE = RepeatedLogThrottle(
    window_seconds=_REDUCTION_AVAILABILITY_LOG_WINDOW_SECONDS,
)


class OrderPersistenceError(RuntimeError):
    """Raised when a broker order was submitted but could not be persisted locally."""


@dataclass(frozen=True, slots=True)
class BrokerSubmissionUncertainError(RuntimeError):
    action: str
    symbol: str
    cause: str

    def __str__(self) -> str:
        return f"{self.action} {self.symbol} broker submission failed: {self.cause}"


_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_FAILED_ORDER_STATUSES = {"REJECTED", "CANCELLED"}
ORDER_EXECUTION_BLOCKED_PREFIX = "ORDER_EXECUTION_BLOCKED:"
ORDER_PERSISTENCE_UNCERTAIN_PREFIX = "ORDER_PERSISTENCE_UNCERTAIN:"
ORDER_STATUS_PERSISTENCE_UNCERTAIN_PREFIX = "ORDER_STATUS_PERSISTENCE_UNCERTAIN:"
PNL_RECONCILIATION_UNCERTAIN_PREFIX = "PNL_RECONCILIATION_UNCERTAIN:"
_TERMINAL_STATUS_PERSIST_BACKOFF_SECONDS = (0.02, 0.05, 0.1)
_TRANSIENT_WRITE_CONFLICT_MARKERS = ("database is locked", "database table is locked")


def _is_transient_write_conflict(exc: BaseException) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in _TRANSIENT_WRITE_CONFLICT_MARKERS)
CALENDAR_COVERAGE_EXPIRED_REASON = "CALENDAR_COVERAGE_EXPIRED"
_OPERATIONAL_PAUSE_PREFIXES = (
    "ORDER_SUBMISSION_UNCERTAIN:",
    "POSITION_RECONCILIATION_UNCERTAIN:",
    "REDUCTION_SETTLEMENT_UNCERTAIN:",
    "ORDER_RECONCILIATION_UNCERTAIN:",
    ORDER_EXECUTION_BLOCKED_PREFIX,
    ORDER_PERSISTENCE_UNCERTAIN_PREFIX,
    ORDER_STATUS_PERSISTENCE_UNCERTAIN_PREFIX,
    PNL_RECONCILIATION_UNCERTAIN_PREFIX,
)
_SKIPPED_ORDER_STATUS = "SKIPPED"
_ENTRY_ACTIONS = {"BUY", "SELL_SHORT"}
_POSITION_REDUCING_ACTIONS = {"SELL", "BUY_TO_COVER"}
_ACTION_TO_SIDE: Final[dict[str, str]] = {
    "BUY": "BUY",
    "SELL": "SELL",
    "SELL_SHORT": "SELL",
    "BUY_TO_COVER": "BUY",
}
ENTRY_BUYING_POWER_USAGE = Decimal("0.9")
US_PRICE_TICK = Decimal("0.01")
RISK_BOUNDARY_VERSION: Final[str] = "pre-submit-risk-v1"

# HKEX stepped tick table (https://www.hkex.com.hk/Services/Trading/Securities/Overview/Trading-Mechanism)
# Phase 2 tick sizes (effective 2019-07); the 20–100 band merged to 0.050.
# Ordered ascending by upper bound; the matching tier is the first whose
# upper bound is strictly greater than the price.
_HK_TICK_TABLE: list[tuple[Decimal, Decimal]] = [
    (Decimal("0.25"), Decimal("0.001")),
    (Decimal("0.50"), Decimal("0.005")),
    (Decimal("10.00"), Decimal("0.010")),
    (Decimal("20.00"), Decimal("0.020")),
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
_NotifyRiskEvent = Callable[[str, str], object]
_RecordOrderSkipped = Callable[[str, str, str, dict[str, object]], None]


@dataclass(frozen=True)
class OrderStatus:
    broker_order_id: str
    status: str
    executed_quantity: Optional[Decimal] = None
    executed_price: Optional[Decimal] = None
    reason: str = ""
    fill_finalized: bool = False
    actual_fee: Optional[Decimal] = None
    fee_currency: str = ""
    broker_submitted_at: datetime | None = None
    broker_updated_at: datetime | None = None

    @staticmethod
    def _positive(value: Optional[Decimal]) -> Decimal:
        """Return ``value`` if it is a positive Decimal, else ``Decimal(0)``.

        Use this everywhere a downstream comparison or multiplication would
        otherwise raise ``TypeError`` against ``None`` (the natural state
        when the broker hasn't reported a fill yet)."""
        if value is None:
            return Decimal("0")
        return value if value > 0 else Decimal("0")


@dataclass(frozen=True)
class FinalOrderQuoteCheckResult:
    """Fresh quote validation result returned immediately before submission."""

    executable_price: Decimal | None = None
    issue: str = ""
    bid: Decimal | None = None
    ask: Decimal | None = None


@dataclass(frozen=True)
class EntryPolicyCheckResult:
    """Entry policy decision evaluated before broker-dependent safety checks."""

    issue: str = ""
    skip_category: str = "RISK"
    details: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _PendingOrder:
    broker: BrokerGateway
    broker_order_id: str
    symbol: str
    action: str
    quantity: Decimal
    price: Decimal
    engine_snapshot: EngineSnapshot | None
    avg_price: Decimal | None = None
    pnl_fee_rate: Decimal = Decimal("0")
    next_status_check_at: float = 0.0
    submitted_at: float = 0.0
    restore_engine_snapshot_fn: Callable[[EngineSnapshot], None] | None = None
    timeout_recovery_attempted: bool = False
    known_terminal_status: str = ""


@dataclass
class _TrackedEntry:
    quantity: Decimal = Decimal("0")
    cost: Decimal = Decimal("0")
    side: str = "LONG"
    opened_at: datetime | None = None

    @property
    def avg_price(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return self.cost / self.quantity


@dataclass(frozen=True)
class TrackedPositionSnapshot:
    symbol: str
    side: str
    quantity: Decimal
    cost: Decimal
    opened_at: datetime | None

    @property
    def avg_price(self) -> Decimal:
        if self.quantity <= 0:
            return Decimal("0")
        return self.cost / self.quantity


@dataclass(frozen=True)
class _EntryPositionCheck:
    current_quantity: Decimal
    conflicting_symbol: str = ""


@dataclass(frozen=True, slots=True)
class _EntryRiskLimits:
    max_quantity: Decimal
    max_notional: Decimal
    max_risk: Decimal
    stop_loss_pct: Decimal


@dataclass(frozen=True, slots=True)
class _PreSubmitRiskRequest:
    action: str
    symbol: str
    quantity: Decimal
    price: Decimal


@dataclass(frozen=True, slots=True)
class ApprovedOrder:
    action: str
    symbol: str
    side: str
    quantity: Decimal
    price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    protective_commit_required: bool = False


_EntryPersistCallback = Callable[[str, Decimal, Decimal], None]
_FillCallback = Callable[[str, str], None]
_ReductionFillCallback = Callable[[str, str, Decimal], None]
_FinalOrderQuoteCheck = Callable[
    ["BrokerGateway", str, str, Decimal],
    FinalOrderQuoteCheckResult | str | None,
]
EntryPolicyCheck = Callable[
    [str, str, str],
    EntryPolicyCheckResult | str | None,
]
_FinalProtectiveExitCheck = Callable[
    ["BrokerGateway", str, str, Decimal, Mapping[str, object]],
    str | None,
]
_FinalProtectiveExitCommitCheck = _FinalProtectiveExitCheck


class _PreSubmitRiskCheckRecorder(Protocol):
    def record_pre_submit_risk_check(self) -> None: ...


class _TerminalCallbackStore(Protocol):
    def claim(self, broker_order_id: str, terminal_status: str) -> bool: ...

    def complete(self, broker_order_id: str, terminal_status: str) -> None: ...

    def release(self, broker_order_id: str, terminal_status: str) -> None: ...


class TradeExecutionService:
    def __init__(
        self,
        record_order: Callable[..., None],
        update_order_status: Callable[..., None],
        record_risk_event: Callable[..., None],
        record_order_skipped: _RecordOrderSkipped | None = None,
        persist_entry: _EntryPersistCallback | None = None,
        on_fill: _FillCallback | None = None,
        on_reduction_fill: _ReductionFillCallback | None = None,
        audit: AuditLogger | None = None,
        decision_funnel: _PreSubmitRiskCheckRecorder | None = None,
        margin_safety_factor: float | None = None,
        allow_position_addons: bool = False,
        short_entries_enabled: bool = False,
        max_position_quantity: int | None = None,
        max_position_notional: float | None = None,
        max_risk_per_trade: float | None = None,
        stop_loss_pct: float | None = None,
        full_buying_power_usage_enabled: bool = False,
        entry_cutoff_minutes_before_close: int = 0,
        final_order_quote_check: _FinalOrderQuoteCheck | None = None,
        entry_policy_check: EntryPolicyCheck | None = None,
        final_protective_exit_check: _FinalProtectiveExitCheck | None = None,
        final_protective_exit_commit_check: (
            _FinalProtectiveExitCommitCheck | None
        ) = None,
        terminal_callback_store: _TerminalCallbackStore | None = None,
    ) -> None:
        self._record_order = record_order
        self._update_order_status = update_order_status
        self._record_order_accepts_metadata = self._accepts_positional_args(
            record_order, 10
        )
        self._update_order_accepts_metadata = self._accepts_positional_args(
            update_order_status, 6
        )
        self._record_risk_event = record_risk_event
        self._record_order_skipped = record_order_skipped
        self._persist_entry = persist_entry
        self._on_fill = on_fill
        self._on_reduction_fill = on_reduction_fill
        self._audit = audit
        self.decision_funnel = decision_funnel
        self.margin_safety_factor = margin_safety_factor
        self.allow_position_addons = allow_position_addons
        self.short_entries_enabled = short_entries_enabled
        self.max_position_quantity = max_position_quantity
        self.max_position_notional = max_position_notional
        self.max_risk_per_trade = max_risk_per_trade
        self.stop_loss_pct = stop_loss_pct
        self.full_buying_power_usage_enabled = full_buying_power_usage_enabled
        self.entry_cutoff_minutes_before_close = entry_cutoff_minutes_before_close
        self._final_order_quote_check = final_order_quote_check
        self._entry_policy_check = entry_policy_check
        self._final_protective_exit_check = final_protective_exit_check
        self._final_protective_exit_commit_check = (
            final_protective_exit_commit_check
        )
        self._terminal_callback_store = terminal_callback_store
        self._state_lock = RLock()
        self._submission_lock = RLock()
        self._pending_orders: dict[str, _PendingOrder] = {}
        self._pending_orders_by_id: dict[str, _PendingOrder] = {}
        self._order_status_poll_interval_seconds = 1.0
        self._order_status_timeout_seconds = 30.0
        self._entry_positions: dict[str, _TrackedEntry] = {}
        self._reconcile_in_flight: set[str] = set()
        self._pending_status_query_warned_ids: set[str] = set()
        self._fill_finalization_in_flight: set[str] = set()
        self._finalized_order_ids: set[str] = set()
        self._active_execution_context: dict[str, object] = {}

    @staticmethod
    def _accepts_positional_args(callback: Callable[..., object], count: int) -> bool:
        try:
            inspect.signature(callback).bind(*([None] * count))
            return True
        except (TypeError, ValueError):
            return False

    @contextmanager
    def submission_guard(self) -> Iterator[None]:
        """Serialize external broker synchronization with order submission."""
        with self._submission_lock:
            yield

    def load_tracked_entries(
        self,
        entries: Mapping[
            str,
            tuple[Decimal, Decimal] | tuple[Decimal, Decimal, str, datetime | None],
        ],
    ) -> None:
        """Restore tracked entry positions (typically at runner startup)."""
        with self._state_lock:
            self._entry_positions.clear()
            for symbol, values in entries.items():
                quantity, cost = values[0], values[1]
                if quantity <= 0 or cost <= 0:
                    continue
                side = values[2] if len(values) >= 3 else "LONG"
                opened_at = values[3] if len(values) >= 4 else None
                self._entry_positions[symbol] = _TrackedEntry(
                    quantity=quantity,
                    cost=cost,
                    side=str(side or "").upper(),
                    opened_at=opened_at,
                )

    def refresh_pending_brokers(self, broker: BrokerGateway) -> None:
        with self._state_lock:
            refreshed: dict[str, _PendingOrder] = {}
            for order_id, pending in self._pending_orders_by_id.items():
                refreshed[order_id] = _PendingOrder(
                    broker=broker,
                    broker_order_id=pending.broker_order_id,
                    symbol=pending.symbol,
                    action=pending.action,
                    quantity=pending.quantity,
                    price=pending.price,
                    engine_snapshot=pending.engine_snapshot,
                    avg_price=pending.avg_price,
                    pnl_fee_rate=pending.pnl_fee_rate,
                    next_status_check_at=pending.next_status_check_at,
                    submitted_at=pending.submitted_at,
                    restore_engine_snapshot_fn=pending.restore_engine_snapshot_fn,
                    timeout_recovery_attempted=pending.timeout_recovery_attempted,
                )
            self._pending_orders_by_id = refreshed
            self._rebuild_pending_orders_by_symbol_locked()

    def _rebuild_pending_orders_by_symbol_locked(self) -> None:
        self._pending_orders = {}
        for pending in self._pending_orders_by_id.values():
            self._pending_orders.setdefault(pending.symbol, pending)

    def load_pending_orders(self, pending_orders: list[_PendingOrder]) -> None:
        with self._state_lock:
            existing_by_id = dict(self._pending_orders_by_id)
            # Build new set from DB results. Preserve in-memory pendings that are NOT
            # in the new list (e.g. just flipped to FILLED by sync) — they will be
            # finalized by the next reconcile cycle rather than silently dropped.
            merged_by_id: dict[str, _PendingOrder] = {}
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
                        pnl_fee_rate=(
                            existing.pnl_fee_rate
                            if existing.pnl_fee_rate > 0
                            else pending.pnl_fee_rate
                        ),
                        next_status_check_at=existing.next_status_check_at,
                        submitted_at=existing.submitted_at,
                        restore_engine_snapshot_fn=existing.restore_engine_snapshot_fn if existing.restore_engine_snapshot_fn is not None else pending.restore_engine_snapshot_fn,
                        timeout_recovery_attempted=existing.timeout_recovery_attempted,
                    )
                merged_by_id[pending.broker_order_id] = pending

            for broker_order_id, existing in existing_by_id.items():
                if broker_order_id not in merged_by_id:
                    merged_by_id[broker_order_id] = existing
                    logger.warning(
                        "in-memory pending order %s for %s not in DB list, preserving for reconcile",
                        broker_order_id,
                        existing.symbol,
                    )

            self._pending_orders_by_id = merged_by_id
            self._rebuild_pending_orders_by_symbol_locked()

    def snapshot_tracked_entries(self) -> dict[str, tuple[Decimal, Decimal]]:
        with self._state_lock:
            return {
                symbol: (entry.quantity, entry.cost)
                for symbol, entry in self._entry_positions.items()
            }

    def tracked_position(self, symbol: str) -> TrackedPositionSnapshot | None:
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            if entry is None or entry.quantity <= 0 or entry.cost <= 0:
                return None
            return TrackedPositionSnapshot(
                symbol=symbol,
                side=entry.side,
                quantity=entry.quantity,
                cost=entry.cost,
                opened_at=entry.opened_at,
            )

    def update_tracked_position_metadata(
        self,
        symbol: str,
        *,
        side: str,
        opened_at: datetime | None = None,
    ) -> None:
        normalized_side = str(side or "").upper()
        if normalized_side not in {"LONG", "SHORT"}:
            return
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            if entry is None:
                return
            entry.side = normalized_side
            if entry.opened_at is None and opened_at is not None:
                entry.opened_at = opened_at

    @property
    def has_pending_order(self) -> bool:
        with self._state_lock:
            return bool(self._pending_orders_by_id)

    @property
    def pending_order(self) -> _PendingOrder | None:
        with self._state_lock:
            return next(iter(self._pending_orders_by_id.values()), None)

    def pending_order_ids(self) -> list[str]:
        with self._state_lock:
            return sorted(self._pending_orders_by_id)

    def broker_uncertain_order_ids(self) -> list[str]:
        """Return pending orders whose broker state is genuinely unknown.

        Excludes orders the broker already reported terminal, which stay
        queued only to retry the local write.
        """
        with self._state_lock:
            return sorted(
                order_id
                for order_id, pending in self._pending_orders_by_id.items()
                if not pending.known_terminal_status
            )

    def pending_order_inventory(self) -> dict[str, list[str]]:
        with self._state_lock:
            inventory: dict[str, list[str]] = {}
            for pending in self._pending_orders_by_id.values():
                inventory.setdefault(pending.symbol, []).append(pending.broker_order_id)
            return {
                symbol: sorted(set(order_ids))
                for symbol, order_ids in sorted(inventory.items())
            }

    def pending_orders_for(self, symbol: str) -> list[_PendingOrder]:
        with self._state_lock:
            return [
                pending
                for pending in self._pending_orders_by_id.values()
                if pending.symbol == symbol
            ]

    def pending_order_by_broker_id(self, order_id: str) -> _PendingOrder | None:
        with self._state_lock:
            return self._pending_orders_by_id.get(order_id)

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
                # Only clear a single order (the first one) rather than all,
                # consistent with the single-order getter semantics.
                first_order_id = next(iter(self._pending_orders_by_id), None)
                if first_order_id is not None:
                    self._pending_orders_by_id.pop(first_order_id, None)
                    self._rebuild_pending_orders_by_symbol_locked()
                return
            for order_id, existing in list(self._pending_orders_by_id.items()):
                if existing.symbol == pending.symbol:
                    self._pending_orders_by_id.pop(order_id, None)
            self._pending_orders_by_id[pending.broker_order_id] = pending
            self._rebuild_pending_orders_by_symbol_locked()

    def reconcile(
        self,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        with self._submission_lock:
            self._reconcile_under_submission_guard(
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )

    def _reconcile_under_submission_guard(
        self,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        with self._state_lock:
            pending_orders = list(self._pending_orders_by_id.values())
        for pending in pending_orders:
            with self._state_lock:
                if pending.broker_order_id not in self._pending_orders_by_id:
                    continue
                if pending.broker_order_id in self._reconcile_in_flight:
                    continue
                self._reconcile_in_flight.add(pending.broker_order_id)
            try:
                self._reconcile_pending_order(
                    pending,
                    risk=risk,
                    notifier=notifier,
                    restore_engine_snapshot=restore_engine_snapshot,
                    notify_risk_event=notify_risk_event,
                )
            finally:
                with self._state_lock:
                    self._reconcile_in_flight.discard(pending.broker_order_id)

    def cancel_pending_order(
        self,
        *,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
    ) -> OrderStatus:
        with self._submission_lock:
            with self._state_lock:
                pending = next(iter(self._pending_orders_by_id.values()), None)
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
        broker_order_id: str | None = None,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus:
        with self._submission_lock:
            return self._cancel_pending_order_for_symbol_under_submission_guard(
                symbol,
                broker_order_id=broker_order_id,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )

    def _cancel_pending_order_for_symbol_under_submission_guard(
        self,
        symbol: str,
        *,
        broker_order_id: str | None = None,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = (
                self._pending_orders_by_id.get(broker_order_id)
                if broker_order_id is not None
                else self._pending_orders.get(symbol)
            )
            if pending is not None and pending.symbol != symbol:
                pending = None
            if pending is None:
                return OrderStatus("", "NO_PENDING_ORDER")
            if pending.broker_order_id in self._reconcile_in_flight:
                return OrderStatus(pending.broker_order_id, "RECONCILE_IN_FLIGHT")
            self._reconcile_in_flight.add(pending.broker_order_id)

        try:
            try:
                order_status = self._coerce_order_status(
                    pending.broker.cancel_order(pending.broker_order_id),
                    pending.broker_order_id,
                )
            except Exception:
                logger.exception("failed to cancel pending order %s", pending.broker_order_id)
                return OrderStatus(pending.broker_order_id, "CANCEL_FAILED")

            status_persisted = self._safe_update_order_status_from_result(order_status)
            if (
                order_status.status in {"FILLED", *_FAILED_ORDER_STATUSES}
                and not status_persisted
            ):
                self._pause_for_order_status_persistence_failure(
                    pending,
                    order_status.status,
                    risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                return order_status

            if order_status.status not in {"FILLED", *_FAILED_ORDER_STATUSES}:
                # A cancel request may be accepted asynchronously. Until the
                # broker reports a terminal state, the original order can still
                # fill and must remain the sole pending order for this symbol.
                self._defer_pending_status_retry(pending, time.monotonic())
                logger.warning(
                    "cancel not terminal for %s: status=%s; keeping pending",
                    pending.broker_order_id,
                    order_status.status,
                )
                return order_status

            fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
            if fill_qty > 0:
                self._finalize_pending_fill(
                    pending, order_status,
                    risk=risk,
                    notifier=notifier,
                    fill_qty=fill_qty,
                    notify_risk_event=notify_risk_event,
                )

            self._clear_pending_order(pending.broker_order_id)
            effective_restore = pending.restore_engine_snapshot_fn or restore_engine_snapshot
            if (
                order_status.status != "FILLED"
                and effective_restore is not None
                and pending.engine_snapshot is not None
            ):
                if fill_qty == 0 or self._should_restore_after_partial_terminal_fill(pending, fill_qty):
                    effective_restore(pending.engine_snapshot)
            logger.info("pending order cancelled: %s status=%s", pending.broker_order_id, order_status.status)
            return order_status
        finally:
            with self._state_lock:
                self._reconcile_in_flight.discard(pending.broker_order_id)

    def cancel_order_by_id(
        self,
        order_id: str,
        broker: BrokerGateway,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus:
        with self._submission_lock:
            return self._cancel_order_by_id_under_submission_guard(
                order_id,
                broker,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )

    def _cancel_order_by_id_under_submission_guard(
        self,
        order_id: str,
        broker: BrokerGateway,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> OrderStatus:
        with self._state_lock:
            pending = self._pending_orders_by_id.get(order_id)
        if pending is not None:
            return self.cancel_pending_order_for_symbol(
                pending.symbol,
                broker_order_id=order_id,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
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
        expected_exit_price: Decimal | float | int | None = None,
        entry_reference_quantity: Decimal | float | int | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        reduce_only: bool = False,
        execution_context: Mapping[str, object] | None = None,
        entry_policy_check: EntryPolicyCheck | None = None,
        allow_opening_warmup_entry: bool = False,
    ) -> OrderStatus | None:
        with self._submission_lock:
            self._active_execution_context = dict(execution_context or {})
            self._active_execution_context.setdefault("market", market)
            self._active_execution_context.setdefault("fee_rate", float(fee_rate))
            if expected_exit_price is not None:
                self._active_execution_context.setdefault(
                    "expected_exit_price",
                    float(expected_exit_price),
                )
            if entry_reference_quantity is not None:
                self._active_execution_context.setdefault(
                    "entry_reference_quantity",
                    float(entry_reference_quantity),
                )
            try:
                return self._execute_under_submission_guard(
                    action,
                    symbol,
                    quote,
                    broker,
                    risk,
                    notifier,
                    cash_currency,
                    market=market,
                    trading_session_mode=trading_session_mode,
                    min_profit_amount=min_profit_amount,
                    allow_loss_exit=allow_loss_exit,
                    fee_rate=fee_rate,
                    expected_exit_price=expected_exit_price,
                    entry_reference_quantity=entry_reference_quantity,
                    engine_snapshot=engine_snapshot,
                    restore_engine_snapshot=restore_engine_snapshot,
                    notify_risk_event=notify_risk_event,
                    reduce_only=reduce_only,
                    entry_policy_check=entry_policy_check,
                    allow_opening_warmup_entry=(
                        allow_opening_warmup_entry
                    ),
                )
            finally:
                self._active_execution_context = {}

    def _execute_under_submission_guard(
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
        expected_exit_price: Decimal | float | int | None = None,
        entry_reference_quantity: Decimal | float | int | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        reduce_only: bool = False,
        entry_policy_check: EntryPolicyCheck | None = None,
        allow_opening_warmup_entry: bool = False,
    ) -> OrderStatus | None:
        if reduce_only and action not in _POSITION_REDUCING_ACTIONS:
            return self._skip_order(
                symbol,
                action,
                "reduce-only execution rejects position-increasing action",
                skip_category="POSITION",
            )
        if action == "SELL_SHORT" and not self.short_entries_enabled:
            return self._skip_order(
                symbol,
                action,
                "short entries are disabled by the live safety policy",
                skip_category="RISK",
            )
        if (
            action == "BUY"
            and trading_session_mode == "ANY"
            and not is_trading_hours(market)
        ):
            return self._skip_order(
                symbol,
                action,
                f"non-trading hours for {market}; ANY mode cannot open a long position",
                skip_category="SESSION",
            )
        if trading_session_mode == "RTH_ONLY":
            if not is_trading_hours(market):
                # SESSION skip records ORDER_SKIPPED only; TRADING_SESSION_BLOCKED is layer A.
                return self._skip_order(
                    symbol,
                    action,
                    f"non-RTH for {market}",
                    skip_category="SESSION",
                )
            if action in _ENTRY_ACTIONS and is_opening_warmup(
                market,
                settings.trading_open_warmup_minutes,
            ) and not allow_opening_warmup_entry:
                return self._skip_order(
                    symbol,
                    action,
                    f"opening warmup for {market}",
                    skip_category="SESSION",
                )
        if action in _ENTRY_ACTIONS and is_closing_window(
            market,
            self.entry_cutoff_minutes_before_close,
        ):
            return self._skip_order(
                symbol,
                action,
                f"entry cutoff within {self.entry_cutoff_minutes_before_close} minutes of close",
                skip_category="SESSION",
            )

        risk_result = risk.check()
        if not risk_result.approved and not self._risk_rejection_allows_action(
            action,
            risk,
            reduce_only=reduce_only,
        ):
            logger.warning("execute rejected by risk: %s", risk_result.reason)
            return self._skip_order(symbol, action, risk_result.reason, skip_category="RISK")
        if not risk_result.approved:
            logger.info("allowing position-reducing %s despite risk rejection: %s", action, risk_result.reason)

        if action in _ENTRY_ACTIONS:
            policy_check = entry_policy_check or self._entry_policy_check
            policy_rejection = self._entry_policy_rejection(
                policy_check,
                symbol,
                action,
                market,
            )
            if policy_rejection is not None:
                return policy_rejection

            unresolved_order_ids = self.pending_order_ids()
            if unresolved_order_ids:
                return self._skip_order(
                    symbol,
                    action,
                    "live or unresolved broker orders block all new entries: "
                    + ", ".join(unresolved_order_ids),
                    skip_category="PENDING",
                )
            safety_error = self._entry_safety_configuration_error()
            if safety_error is not None:
                return self._skip_order(
                    symbol,
                    action,
                    safety_error,
                    skip_category="RISK",
                )

            position_check = self._entry_position_check(broker, symbol, action)
            if position_check is None:
                return self._skip_order(
                    symbol,
                    action,
                    "broker position lookup unavailable; entry denied by live safety policy",
                    skip_category="RISK",
                )
            if position_check.conflicting_symbol:
                return self._skip_order(
                    symbol,
                    action,
                    (
                        f"cross-symbol broker position {position_check.conflicting_symbol} "
                        "blocks new entry"
                    ),
                    skip_category="POSITION",
                )
            if not self.allow_position_addons and position_check.current_quantity > 0:
                return self._skip_order(
                    symbol,
                    action,
                    "existing broker or tracked position blocks entry while add-ons are disabled",
                    skip_category="POSITION",
                )

        if action == "BUY":
            if self._is_losing_long_add_on(symbol, Decimal(str(quote.last_price))):
                return self._skip_order(
                    symbol,
                    action,
                    "existing losing long position blocks add-on buy",
                    skip_category="POSITION",
                )

        with self._state_lock:
            pending = self._pending_orders.get(symbol)
            if pending is not None:
                logger.warning("execute skipped: pending order %s still live for %s", pending.broker_order_id, symbol)
                return self._skip_order(symbol, action, "pending order in flight", skip_category="PENDING")

        if action == "BUY":
            return self._execute_buy(
                symbol,
                quote,
                broker,
                risk,
                notifier,
                cash_currency,
                min_profit_amount=min_profit_amount,
                fee_rate=fee_rate,
                expected_exit_price=expected_exit_price,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
                final_entry_policy_check=entry_policy_check,
                market=market,
            )
        if action == "SELL":
            return self._execute_sell(
                symbol,
                quote,
                broker,
                risk,
                notifier,
                min_profit_amount=min_profit_amount,
                allow_loss_exit=allow_loss_exit,
                fee_rate=fee_rate,
                entry_reference_quantity=entry_reference_quantity,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
                reduce_only=reduce_only,
            )
        if action == "SELL_SHORT":
            return self._execute_sell_short(
                symbol,
                quote,
                broker,
                risk,
                notifier,
                cash_currency,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
                final_entry_policy_check=entry_policy_check,
                market=market,
            )
        if action == "BUY_TO_COVER":
            return self._execute_buy_to_cover(
                symbol,
                quote,
                broker,
                risk,
                notifier,
                min_profit_amount=min_profit_amount,
                allow_loss_exit=allow_loss_exit,
                fee_rate=fee_rate,
                entry_reference_quantity=entry_reference_quantity,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
                reduce_only=reduce_only,
            )
        logger.warning("unknown action: %s", action)
        return None

    def _entry_policy_rejection(
        self,
        policy_check: EntryPolicyCheck | None,
        symbol: str,
        action: str,
        market: str,
    ) -> OrderStatus | None:
        if policy_check is None:
            return None
        try:
            policy_result = policy_check(symbol, action, market)
        except Exception:
            logger.exception(
                "entry policy check unavailable for %s %s",
                action,
                symbol,
            )
            return self._skip_order(
                symbol,
                action,
                "entry policy check unavailable; entry denied",
                skip_category="RISK",
            )
        if isinstance(policy_result, str):
            if not policy_result:
                return None
            return self._skip_order(
                symbol,
                action,
                policy_result,
                skip_category="RISK",
            )
        if policy_result is None or not policy_result.issue:
            return None
        policy_details = dict(policy_result.details)
        policy_details.pop("skip_category", None)
        return self._skip_order(
            symbol,
            action,
            policy_result.issue,
            skip_category=policy_result.skip_category,
            **policy_details,
        )

    @staticmethod
    def _risk_rejection_allows_action(
        action: str,
        risk: RiskController,
        *,
        reduce_only: bool,
    ) -> bool:
        if action not in _POSITION_REDUCING_ACTIONS or risk.kill_switch:
            return False
        if risk.paused and risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES):
            return reduce_only and risk.protective_exit_permitted
        return True

    def _is_losing_long_add_on(self, symbol: str, price: Decimal) -> bool:
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            if entry is None or entry.quantity <= 0:
                return False
            avg_price = entry.avg_price
        return avg_price > 0 and price < avg_price

    @staticmethod
    def _positive_finite_limit(
        value: Decimal | float | int | None,
    ) -> Decimal | None:
        if value is None:
            return None
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not parsed.is_finite() or parsed <= 0:
            return None
        return parsed

    def _entry_risk_limits(self) -> _EntryRiskLimits | str:
        with self._state_lock:
            raw_max_quantity = self.max_position_quantity
            raw_max_notional = self.max_position_notional
            raw_max_risk = self.max_risk_per_trade
            raw_stop_loss_pct = self.stop_loss_pct

        max_quantity = self._positive_finite_limit(raw_max_quantity)
        if max_quantity is None:
            return (
                "invalid live safety limit: max_position_quantity must be "
                "configured, finite, and greater than zero"
            )
        max_notional = self._positive_finite_limit(raw_max_notional)
        if max_notional is None:
            return (
                "invalid live safety limit: max_position_notional must be "
                "configured, finite, and greater than zero"
            )
        max_risk = self._positive_finite_limit(raw_max_risk)
        if max_risk is None:
            return (
                "invalid live safety limit: max_risk_per_trade must be "
                "configured, finite, and greater than zero"
            )
        stop_loss_pct = self._positive_finite_limit(raw_stop_loss_pct)
        if stop_loss_pct is None:
            return (
                "invalid live safety limit: stop_loss_pct must be configured, "
                "finite, and greater than zero"
            )
        return _EntryRiskLimits(
            max_quantity=max_quantity,
            max_notional=max_notional,
            max_risk=max_risk,
            stop_loss_pct=stop_loss_pct,
        )

    def _entry_safety_configuration_error(self) -> str | None:
        match self._entry_risk_limits():
            case str() as issue:
                return issue
            case _EntryRiskLimits():
                return None
            case unreachable:
                assert_never(unreachable)

    def _pre_submit_risk_rejection(
        self,
        request: _PreSubmitRiskRequest,
        reason: str,
    ) -> OrderStatus:
        message = (
            "PRE_SUBMIT_RISK_CHECK_REJECTED: "
            f"{request.action} {request.symbol}: {reason}"
        )
        try:
            self._record_risk_event(message)
        except Exception as exc:
            logger.error(
                "pre-submit risk-event persistence failed for %s %s: %s",
                request.action,
                request.symbol,
                exc,
            )
            message = (
                f"{message}; risk-event persistence unavailable: "
                f"{type(exc).__name__}"
            )
        return self._skip_order(
            request.symbol,
            request.action,
            message,
            skip_category="RISK",
        )

    def pre_submit_risk_check(
        self,
        request: _PreSubmitRiskRequest,
        broker: BrokerGateway,
    ) -> OrderStatus | ApprovedOrder:
        if self.decision_funnel is not None:
            self.decision_funnel.record_pre_submit_risk_check()

        if request.action in _POSITION_REDUCING_ACTIONS:
            return ApprovedOrder(
                action=request.action,
                symbol=request.symbol,
                side=_ACTION_TO_SIDE[request.action],
                quantity=request.quantity,
                price=request.price,
            )
        if request.action not in _ENTRY_ACTIONS:
            return self._pre_submit_risk_rejection(
                request,
                "unknown position-increasing action",
            )
        if request.action == "SELL_SHORT":
            return self._pre_submit_risk_rejection(
                request,
                "short entries are disabled by the mandatory risk boundary",
            )
        # Past the published closure horizon the calendar cannot tell a holiday
        # from a trading day, so an entry here would be placed blind. Reductions
        # already returned above, keeping exits, stops and the end-of-day
        # flatten alive while new risk is refused.
        entry_market = market_for_symbol(request.symbol)
        if is_coverage_expired(entry_market, trade_day_for(entry_market)):
            return self._pre_submit_risk_rejection(
                request,
                f"{CALENDAR_COVERAGE_EXPIRED_REASON} for {entry_market}",
            )

        limits_result = self._entry_risk_limits()
        match limits_result:
            case str() as issue:
                return self._pre_submit_risk_rejection(request, issue)
            case _EntryRiskLimits() as limits:
                pass
            case unreachable:
                assert_never(unreachable)

        if not request.quantity.is_finite() or request.quantity <= 0:
            return self._pre_submit_risk_rejection(
                request,
                "entry quantity must be finite and greater than zero",
            )
        if not request.price.is_finite() or request.price <= 0:
            return self._pre_submit_risk_rejection(
                request,
                "entry price must be fresh, finite, and greater than zero",
            )

        position_check = self._entry_position_check(
            broker,
            request.symbol,
            request.action,
        )
        if position_check is None:
            return self._pre_submit_risk_rejection(
                request,
                "broker position state is unavailable or uncertain",
            )
        if position_check.conflicting_symbol:
            return self._pre_submit_risk_rejection(
                request,
                f"cross-symbol position {position_check.conflicting_symbol} is open",
            )
        if position_check.current_quantity > 0:
            return self._pre_submit_risk_rejection(
                request,
                "existing position blocks entry at the mandatory risk boundary",
            )

        quote_check = self._final_order_quote_check
        if quote_check is None:
            return self._pre_submit_risk_rejection(
                request,
                "fresh executable quote validation is unavailable",
            )
        try:
            quote_result = quote_check(
                broker,
                request.symbol,
                request.action,
                request.price,
            )
        except Exception as exc:
            return self._pre_submit_risk_rejection(
                request,
                "fresh executable quote validation failed: "
                f"{type(exc).__name__}",
            )
        match quote_result:
            case FinalOrderQuoteCheckResult(
                executable_price=fresh_price,
                issue=quote_issue,
                bid=bid,
                ask=ask,
            ):
                if quote_issue:
                    return self._pre_submit_risk_rejection(request, quote_issue)
            case str() as quote_issue:
                return self._pre_submit_risk_rejection(
                    request,
                    quote_issue or "fresh executable quote is unavailable",
                )
            case None:
                return self._pre_submit_risk_rejection(
                    request,
                    "fresh executable quote is unavailable",
                )
            case unreachable:
                assert_never(unreachable)

        if fresh_price is None or not fresh_price.is_finite() or fresh_price <= 0:
            return self._pre_submit_risk_rejection(
                request,
                "fresh executable price must be finite and greater than zero",
            )

        approved_price = max(request.price, fresh_price)
        projected_quantity = position_check.current_quantity + request.quantity
        if projected_quantity > limits.max_quantity:
            return self._pre_submit_risk_rejection(
                request,
                f"projected quantity {projected_quantity} exceeds cap {limits.max_quantity}",
            )
        projected_notional = projected_quantity * approved_price
        if projected_notional > limits.max_notional:
            return self._pre_submit_risk_rejection(
                request,
                f"projected notional {projected_notional} exceeds cap {limits.max_notional}",
            )
        stop_distance = approved_price * limits.stop_loss_pct / Decimal("100")
        if not stop_distance.is_finite() or stop_distance <= 0:
            return self._pre_submit_risk_rejection(
                request,
                "stop distance is unavailable",
            )
        projected_risk = projected_quantity * stop_distance
        if projected_risk > limits.max_risk:
            return self._pre_submit_risk_rejection(
                request,
                f"projected stop risk {projected_risk} exceeds cap {limits.max_risk}",
            )
        return ApprovedOrder(
            action=request.action,
            symbol=request.symbol,
            side=_ACTION_TO_SIDE[request.action],
            quantity=request.quantity,
            price=approved_price,
            bid=bid,
            ask=ask,
        )

    def _entry_position_check(
        self,
        broker: BrokerGateway,
        symbol: str,
        action: str,
    ) -> _EntryPositionCheck | None:
        tracked = self.tracked_position(symbol)
        current_quantity = tracked.quantity if tracked is not None else Decimal("0")
        position_reader = getattr(broker, "get_positions", None)
        if not callable(position_reader):
            logger.error("%s: broker position lookup is unavailable", action)
            return None

        try:
            broker_quantity = Decimal("0")
            positions = cast("list[object]", position_reader())
            for position in positions:
                quantity = abs(Decimal(str(getattr(position, "quantity", 0))))
                if not quantity.is_finite():
                    raise ValueError("broker position quantity is not finite")
                if quantity <= 0:
                    continue
                position_symbol = str(getattr(position, "symbol", "")).upper()
                if position_symbol != symbol.upper():
                    conflicting_symbol = position_symbol or "<unknown>"
                    logger.error(
                        "%s: cross-symbol broker position %s blocks entry for %s",
                        action,
                        conflicting_symbol,
                        symbol,
                    )
                    return _EntryPositionCheck(
                        current_quantity=current_quantity,
                        conflicting_symbol=conflicting_symbol,
                    )
                broker_quantity += quantity
        except Exception:
            logger.exception("%s: failed to load broker position for live safety checks", action)
            return None
        return _EntryPositionCheck(
            current_quantity=max(current_quantity, broker_quantity),
        )

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
        limits_result = self._entry_risk_limits()
        match limits_result:
            case str() as issue:
                logger.error("%s: %s", side, issue)
                return 0
            case _EntryRiskLimits() as limits:
                pass
            case unreachable:
                assert_never(unreachable)
        if not price.is_finite() or price <= 0:
            logger.error("%s: entry price must be finite and greater than zero", side)
            return 0

        position_check = self._entry_position_check(
            broker,
            symbol,
            "SELL_SHORT" if side == "SELL" else "BUY",
        )
        if position_check is None:
            return 0
        if position_check.conflicting_symbol:
            logger.warning(
                "%s: cross-symbol position %s appeared during final sizing; entry denied",
                side,
                position_check.conflicting_symbol,
            )
            return 0
        current_qty = position_check.current_quantity
        if not self.allow_position_addons and current_qty > 0:
            logger.warning(
                "%s: position appeared during final sizing; add-on denied",
                side,
            )
            return 0

        broker_max_quantity = broker.estimate_margin_max_quantity(
            symbol,
            side,
            price,
            cash_currency,
        )
        max_qty = self._positive_finite_limit(broker_max_quantity)
        if max_qty is None:
            logger.error("%s: broker margin quantity is unavailable", side)
            return 0
        raw_factor = (
            safety_factor
            if safety_factor is not None
            else self.margin_safety_factor
        )
        factor = self._positive_finite_limit(
            ENTRY_BUYING_POWER_USAGE if raw_factor is None else raw_factor
        )
        if factor is None:
            logger.error("%s: buying-power safety factor is invalid", side)
            return 0

        candidate = max_qty * factor
        remaining_qty = limits.max_quantity - current_qty
        candidate = min(candidate, max(Decimal("0"), remaining_qty))

        current_notional = current_qty * price
        remaining_notional = limits.max_notional - current_notional
        notional_qty = max(Decimal("0"), remaining_notional) / price
        candidate = min(candidate, notional_qty)

        stop_distance = price * limits.stop_loss_pct / Decimal("100")
        remaining_risk = max(
            Decimal("0"),
            limits.max_risk - current_qty * stop_distance,
        )
        risk_qty = remaining_risk / stop_distance
        candidate = min(candidate, risk_qty)

        qty = int(candidate)
        if qty <= 0:
            logger.warning(
                "%s: qty <= 0 after live entry sizing, margin_max_qty=%s "
                "price=%s currency=%s factor=%s current_qty=%s",
                side,
                max_qty,
                price,
                cash_currency,
                factor,
                current_qty,
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
    def _normalize_marketable_limit_price(
        symbol: str,
        action: str,
        price: Decimal,
    ) -> Decimal:
        """Round through the executable BBO, never away from it."""
        upper_symbol = symbol.upper()
        rounding = (
            ROUND_CEILING
            if action in {"BUY", "BUY_TO_COVER"}
            else ROUND_FLOOR
        )
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
    def _coerce_non_negative_decimal(value: object) -> Decimal:
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
        entry_reference_quantity: Decimal | float | int | None = None,
    ) -> Decimal:
        buffer_pct = Decimal(str(settings.min_exit_profit_pct or 0)) / Decimal("100")
        pct_profit_amount = avg_price * quantity * buffer_pct
        configured_amount = TradeExecutionService._coerce_non_negative_decimal(min_profit_amount)
        reference_quantity = TradeExecutionService._coerce_non_negative_decimal(
            entry_reference_quantity
        )
        if reference_quantity > 0:
            configured_amount *= min(
                Decimal("1"),
                quantity / reference_quantity,
            )
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
        entry_reference_quantity: Decimal | float | int | None = None,
    ) -> OrderStatus | None:
        if allow_loss_exit or quantity <= 0 or avg_price <= 0:
            return None
        expected_profit = (
            (exit_price - avg_price) * quantity
            if action == "SELL"
            else (avg_price - exit_price) * quantity
        )
        required_profit = self._minimum_required_profit_amount(
            avg_price,
            quantity,
            min_profit_amount,
            entry_reference_quantity,
        )
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

    def _profit_guard_for_entry(
        self,
        *,
        symbol: str,
        entry_price: Decimal,
        expected_exit_price: Decimal | float | int | None,
        quantity: Decimal,
        bid: object,
        ask: object,
        min_profit_amount: Decimal | float | int,
        fee_rate: Decimal | float | int,
    ) -> OrderStatus | None:
        if expected_exit_price is None:
            return None
        target = self._coerce_non_negative_decimal(expected_exit_price)
        if target <= 0:
            return self._skip_order(
                symbol,
                "BUY",
                "expected exit price is unavailable; fee-adjusted entry denied",
                skip_category="FEE",
            )
        try:
            bid_price = Decimal(str(bid))
            ask_price = Decimal(str(ask))
        except Exception:
            bid_price = Decimal("0")
            ask_price = Decimal("0")
        if (
            not bid_price.is_finite()
            or not ask_price.is_finite()
            or bid_price <= 0
            or ask_price < bid_price
        ):
            return self._skip_order(
                symbol,
                "BUY",
                "valid BBO is unavailable; fee-adjusted entry denied",
                skip_category="FEE",
            )

        spread_cost = (ask_price - bid_price) * quantity
        slippage_cost = (
            entry_price
            * quantity
            * Decimal(str(settings.entry_round_trip_slippage_bps))
            / Decimal("10000")
        )
        one_side_rate = self._coerce_non_negative_decimal(fee_rate)
        minimum_profit = self._coerce_non_negative_decimal(
            min_profit_amount
        )
        minimum_profit_pct = Decimal(
            str(settings.min_exit_profit_pct or 0)
        )
        extra_costs = spread_cost + slippage_cost
        edge = evaluate_long_round_trip_edge(
            entry_price=entry_price,
            exit_price=target,
            quantity=quantity,
            one_side_rate=one_side_rate,
            minimum_profit_amount=minimum_profit,
            minimum_profit_pct=minimum_profit_pct,
            extra_costs=extra_costs,
        )
        minimum_ratio = Decimal(
            str(settings.min_entry_edge_cost_ratio)
        )
        minimum_reward_risk_ratio = Decimal(
            str(settings.min_entry_reward_risk_ratio)
        )
        stop_loss_pct = self._coerce_non_negative_decimal(
            self.stop_loss_pct
        )
        stop_price: Decimal | None = None
        stop_gross_loss: Decimal | None = None
        stop_costs: Decimal | None = None
        downside_risk: Decimal | None = None
        reward_risk_ratio: Decimal | None = None
        reward_risk_meets = True
        if stop_loss_pct > 0 and target > entry_price:
            stop_price = (
                entry_price
                * (Decimal("1") - stop_loss_pct / Decimal("100"))
            )
            reward_risk = evaluate_long_round_trip_reward_risk(
                entry_price=entry_price,
                target_exit_price=target,
                stop_exit_price=stop_price,
                quantity=quantity,
                one_side_rate=one_side_rate,
                minimum_profit_amount=minimum_profit,
                minimum_profit_pct=minimum_profit_pct,
                extra_costs=extra_costs,
            )
            edge = reward_risk.target_edge
            stop_gross_loss = -reward_risk.stop_edge.gross_profit
            stop_costs = reward_risk.stop_edge.total_costs
            downside_risk = reward_risk.downside_risk
            reward_risk_ratio = reward_risk.reward_risk_ratio
            reward_risk_meets = reward_risk.meets(
                minimum_reward_risk_ratio
            )
        edge_payload: dict[str, object] = {
            "entry_cost_gate_version": "v2",
            "expected_profit": float(edge.gross_profit),
            "estimated_fees": float(edge.estimated_fees),
            "estimated_spread_cost": float(spread_cost),
            "estimated_slippage_cost": float(slippage_cost),
            "estimated_total_cost": float(edge.total_costs),
            "net_expected_profit": float(edge.net_profit),
            "required_profit": float(edge.required_profit),
            "edge_cost_ratio": (
                float(edge.edge_cost_ratio)
                if edge.edge_cost_ratio is not None
                else None
            ),
            "minimum_edge_cost_ratio": float(minimum_ratio),
            "stop_loss_pct": (
                float(stop_loss_pct) if stop_loss_pct > 0 else None
            ),
            "expected_stop_price": (
                float(stop_price) if stop_price is not None else None
            ),
            "estimated_stop_gross_loss": (
                float(stop_gross_loss)
                if stop_gross_loss is not None
                else None
            ),
            "estimated_stop_costs": (
                float(stop_costs) if stop_costs is not None else None
            ),
            "downside_risk_amount": (
                float(downside_risk)
                if downside_risk is not None
                else None
            ),
            "reward_risk_ratio": (
                float(reward_risk_ratio)
                if reward_risk_ratio is not None
                else None
            ),
            "minimum_reward_risk_ratio": float(
                minimum_reward_risk_ratio
            ),
            "quantity": float(quantity),
            "price": float(entry_price),
            "expected_exit_price": float(target),
        }
        self._active_execution_context.update(edge_payload)
        edge_meets = edge.meets(minimum_ratio)
        if edge_meets and reward_risk_meets:
            return None
        ratio = (
            f"{edge.edge_cost_ratio:.3f}"
            if edge.edge_cost_ratio is not None
            else "unbounded"
        )
        reward_risk_text = (
            f"{reward_risk_ratio:.3f}"
            if reward_risk_ratio is not None
            else "unavailable"
        )
        return self._skip_order(
            symbol,
            "BUY",
            (
                f"fee-adjusted entry net profit {edge.net_profit:.2f} is below "
                f"required minimum profit {edge.required_profit:.2f}, or "
                f"edge/cost ratio {ratio} is below {minimum_ratio:.3f}, or "
                f"reward/risk ratio {reward_risk_text} is below "
                f"{minimum_reward_risk_ratio:.3f}"
            ),
            skip_category="FEE" if not edge_meets else "RISK",
            **edge_payload,
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
        min_profit_amount: Decimal | float | int = Decimal("0"),
        fee_rate: Decimal | float | int = Decimal("0"),
        expected_exit_price: Decimal | float | int | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        final_entry_policy_check: EntryPolicyCheck | None = None,
        market: str = "US",
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "BUY", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return None
        qty = self._entry_quantity_from_margin_power(broker, symbol, "BUY", price, cash_currency)
        if qty <= 0:
            return self._skip_order(
                symbol,
                "BUY",
                "entry quantity is zero after buying-power and position checks",
                skip_category="POSITION",
            )
        entry_guard = self._profit_guard_for_entry(
            symbol=symbol,
            entry_price=price,
            expected_exit_price=expected_exit_price,
            quantity=Decimal(qty),
            bid=quote.bid,
            ask=quote.ask,
            min_profit_amount=min_profit_amount,
            fee_rate=fee_rate,
        )
        if entry_guard is not None:
            return entry_guard

        order_status = self._submit_limit_order(
            "BUY",
            symbol,
            Decimal(qty),
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            entry_expected_exit_price=expected_exit_price,
            entry_min_profit_amount=min_profit_amount,
            entry_fee_rate=fee_rate,
            entry_bid=quote.bid,
            entry_ask=quote.ask,
            final_entry_policy_check=final_entry_policy_check,
            market=market,
        )
        if (
            order_status is None
            or order_status.status != "FILLED"
            or order_status.fill_finalized
        ):
            return order_status

        fill_price = OrderStatus._positive(order_status.executed_price) or price
        fill_qty = OrderStatus._positive(order_status.executed_quantity) or Decimal(qty)
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=order_status.broker_order_id,
            symbol=symbol,
            action="BUY",
            quantity=Decimal(qty),
            price=price,
            engine_snapshot=engine_snapshot,
        )
        self._record_entry_fill(
            pending,
            fill_price,
            fill_qty,
            side="LONG",
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        self._safe_notify_order(
            notifier,
            "BUY",
            symbol,
            str(fill_qty),
            str(fill_price),
            order_status.broker_order_id,
        )
        self._mark_fill_processed(symbol, "BUY")
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
        entry_reference_quantity: Decimal | float | int | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        reduce_only: bool = False,
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
            entry_reference_quantity=entry_reference_quantity,
        )
        if profit_guard is not None:
            return profit_guard

        order_status = self._submit_limit_order(
            "SELL",
            symbol,
            qty,
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            avg_price=pos_avg_price,
            bind_final_executable_price=reduce_only,
            reduce_only=reduce_only,
            exit_min_profit_amount=min_profit_amount,
            exit_allow_loss_exit=allow_loss_exit,
            exit_fee_rate=fee_rate,
            exit_entry_reference_quantity=entry_reference_quantity,
        )
        if (
            order_status is None
            or order_status.status != "FILLED"
            or order_status.fill_finalized
        ):
            return order_status

        fill_price = OrderStatus._positive(order_status.executed_price) or price
        fill_qty = OrderStatus._positive(order_status.executed_quantity) or qty
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=order_status.broker_order_id,
            symbol=symbol,
            action="SELL",
            quantity=qty,
            price=price,
            engine_snapshot=engine_snapshot,
            avg_price=pos_avg_price,
            pnl_fee_rate=self._coerce_non_negative_decimal(fee_rate),
        )
        outcome = self._persist_authoritative_exit_outcome(
            pending,
            order_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fallback_avg_price=pos_avg_price,
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        net_pnl: Decimal | None = None
        if outcome is not None:
            gross_pnl, net_pnl = outcome
            logger.info(
                "SELL: %s qty=%s price=%s avg_price=%s gross_pnl=%s net_pnl=%s",
                symbol,
                fill_qty,
                fill_price,
                pos_avg_price,
                gross_pnl,
                net_pnl,
            )
        else:
            logger.warning(
                "SELL: %s qty=%s price=%s has no authoritative tracked cost; "
                "skipping PnL recording",
                symbol,
                fill_qty,
                fill_price,
            )
        self._settle_reduction_fill(
            pending,
            fill_qty,
            net_pnl=net_pnl,
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        self._safe_notify_order(
            notifier,
            "SELL",
            symbol,
            str(fill_qty),
            str(fill_price),
            order_status.broker_order_id,
        )
        self._mark_fill_processed(symbol, "SELL")
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
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        final_entry_policy_check: EntryPolicyCheck | None = None,
        market: str = "US",
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "SELL_SHORT", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return None

        qty = self._entry_quantity_from_margin_power(broker, symbol, "SELL", price, cash_currency)
        if qty <= 0:
            return self._skip_order(
                symbol,
                "SELL_SHORT",
                "entry quantity is zero after buying-power and position checks",
                skip_category="POSITION",
            )

        order_status = self._submit_limit_order(
            "SELL_SHORT",
            symbol,
            Decimal(qty),
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            final_entry_policy_check=final_entry_policy_check,
            market=market,
        )
        if (
            order_status is None
            or order_status.status != "FILLED"
            or order_status.fill_finalized
        ):
            return order_status

        fill_price = OrderStatus._positive(order_status.executed_price) or price
        fill_qty = OrderStatus._positive(order_status.executed_quantity) or Decimal(qty)
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=order_status.broker_order_id,
            symbol=symbol,
            action="SELL_SHORT",
            quantity=Decimal(qty),
            price=price,
            engine_snapshot=engine_snapshot,
        )
        self._record_entry_fill(
            pending,
            fill_price,
            fill_qty,
            side="SHORT",
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        self._safe_notify_order(
            notifier,
            "SELL_SHORT",
            symbol,
            str(fill_qty),
            str(fill_price),
            order_status.broker_order_id,
        )
        self._mark_fill_processed(symbol, "SELL_SHORT")
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
        entry_reference_quantity: Decimal | float | int | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        reduce_only: bool = False,
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
            entry_reference_quantity=entry_reference_quantity,
        )
        if profit_guard is not None:
            return profit_guard

        order_status = self._submit_limit_order(
            "BUY_TO_COVER",
            symbol,
            qty,
            price,
            broker,
            risk,
            notifier,
            engine_snapshot=engine_snapshot,
            restore_engine_snapshot=restore_engine_snapshot,
            notify_risk_event=notify_risk_event,
            avg_price=pos_avg_price,
            bind_final_executable_price=reduce_only,
            reduce_only=reduce_only,
            exit_min_profit_amount=min_profit_amount,
            exit_allow_loss_exit=allow_loss_exit,
            exit_fee_rate=fee_rate,
            exit_entry_reference_quantity=entry_reference_quantity,
        )
        if (
            order_status is None
            or order_status.status != "FILLED"
            or order_status.fill_finalized
        ):
            return order_status

        fill_price = OrderStatus._positive(order_status.executed_price) or price
        fill_qty = OrderStatus._positive(order_status.executed_quantity) or qty
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=order_status.broker_order_id,
            symbol=symbol,
            action="BUY_TO_COVER",
            quantity=qty,
            price=price,
            engine_snapshot=engine_snapshot,
            avg_price=pos_avg_price,
            pnl_fee_rate=self._coerce_non_negative_decimal(fee_rate),
        )
        outcome = self._persist_authoritative_exit_outcome(
            pending,
            order_status,
            fill_price=fill_price,
            fill_qty=fill_qty,
            fallback_avg_price=pos_avg_price,
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        net_pnl: Decimal | None = None
        if outcome is not None:
            gross_pnl, net_pnl = outcome
            logger.info(
                "BUY_TO_COVER: %s qty=%s price=%s avg_price=%s gross_pnl=%s net_pnl=%s",
                symbol,
                fill_qty,
                fill_price,
                pos_avg_price,
                gross_pnl,
                net_pnl,
            )
        else:
            logger.warning(
                "BUY_TO_COVER: %s qty=%s price=%s has no authoritative tracked cost; "
                "skipping PnL recording",
                symbol,
                fill_qty,
                fill_price,
            )
        self._settle_reduction_fill(
            pending,
            fill_qty,
            net_pnl=net_pnl,
            risk=risk,
            notify_risk_event=notify_risk_event,
        )
        self._safe_notify_order(
            notifier,
            "BUY_TO_COVER",
            symbol,
            str(fill_qty),
            str(fill_price),
            order_status.broker_order_id,
        )
        self._mark_fill_processed(symbol, "BUY_TO_COVER")
        return order_status

    def _submit_limit_order(
        self,
        action: str,
        symbol: str,
        qty: Decimal,
        price: Decimal,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        *,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        avg_price: Decimal | None = None,
        bind_final_executable_price: bool = False,
        reduce_only: bool = False,
        entry_expected_exit_price: Decimal | float | int | None = None,
        entry_min_profit_amount: Decimal | float | int = Decimal("0"),
        entry_fee_rate: Decimal | float | int = Decimal("0"),
        entry_bid: object = None,
        entry_ask: object = None,
        exit_min_profit_amount: Decimal | float | int = Decimal("0"),
        exit_allow_loss_exit: bool = False,
        exit_fee_rate: Decimal | float | int = Decimal("0"),
        exit_entry_reference_quantity: Decimal | float | int | None = None,
        final_entry_policy_check: EntryPolicyCheck | None = None,
        market: str = "US",
    ) -> OrderStatus | None:
        """Submit a limit order, persist it, and handle immediate live/terminal/filled outcomes.

        Returns ``OrderStatus`` for live, terminal, or filled orders.  Callers that
        need post-fill bookkeeping (entry recording, PnL, position consumption)
        should inspect ``status == "FILLED"`` and perform their own tail logic.
        """
        with self._submission_lock:
            precheck_result = self._final_submission_precheck(
                action,
                symbol,
                qty,
                price,
                broker,
                risk,
                bind_final_executable_price=bind_final_executable_price,
                entry_expected_exit_price=entry_expected_exit_price,
                entry_min_profit_amount=entry_min_profit_amount,
                entry_fee_rate=entry_fee_rate,
                entry_bid=entry_bid,
                entry_ask=entry_ask,
                exit_avg_price=avg_price,
                exit_min_profit_amount=exit_min_profit_amount,
                exit_allow_loss_exit=exit_allow_loss_exit,
                exit_fee_rate=exit_fee_rate,
                exit_entry_reference_quantity=exit_entry_reference_quantity,
                final_entry_policy_check=final_entry_policy_check,
                market=market,
                reduce_only=reduce_only,
            )
            if isinstance(precheck_result, OrderStatus):
                return precheck_result

            approved_order = precheck_result
            submit_started_at = datetime.now(timezone.utc)
            submit_started_monotonic = time.perf_counter()

            def submit_approved_order() -> OrderResult:
                try:
                    return broker.submit_limit_order(
                        approved_order.symbol,
                        approved_order.side,
                        approved_order.quantity,
                        approved_order.price,
                    )
                except Exception as exc:
                    raise BrokerSubmissionUncertainError(
                        action=approved_order.action,
                        symbol=approved_order.symbol,
                        cause=str(exc),
                    ) from exc

            if approved_order.protective_commit_required:
                with risk.protective_submission_guard():
                    commit_check = self._final_protective_exit_commit_check
                    if commit_check is None:
                        risk.revoke_protective_exits()
                        return self._skip_order(
                            approved_order.symbol,
                            approved_order.action,
                            "protective exit commit verification is unavailable",
                            skip_category="RISK",
                        )
                    try:
                        protective_issue = commit_check(
                            broker,
                            approved_order.symbol,
                            approved_order.action,
                            approved_order.quantity,
                            dict(self._active_execution_context),
                        )
                    except Exception:
                        logger.exception(
                            "protective exit commit verification failed for %s %s",
                            approved_order.action,
                            approved_order.symbol,
                        )
                        protective_issue = (
                            "protective exit commit verification raised an exception"
                        )
                    if protective_issue is not None:
                        risk.revoke_protective_exits()
                        return self._skip_order(
                            approved_order.symbol,
                            approved_order.action,
                            str(protective_issue),
                            skip_category="RISK",
                        )
                    with risk.protective_permission_guard() as permitted:
                        if permitted:
                            broker_result = submit_approved_order()
                    if not permitted:
                        risk.revoke_protective_exits()
                        return self._skip_order(
                            approved_order.symbol,
                            approved_order.action,
                            "protective exit permission changed at broker commit",
                            skip_category="RISK",
                        )
            else:
                broker_result = submit_approved_order()

            return self._process_submitted_order(
                precheck_result,
                broker_result,
                broker,
                risk,
                notifier,
                submit_started_at=submit_started_at,
                submit_started_monotonic=submit_started_monotonic,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
                avg_price=avg_price,
            )

    def _final_submission_precheck(
        self,
        action: str,
        symbol: str,
        qty: Decimal,
        price: Decimal,
        broker: BrokerGateway,
        risk: RiskController,
        *,
        bind_final_executable_price: bool = False,
        entry_expected_exit_price: Decimal | float | int | None = None,
        entry_min_profit_amount: Decimal | float | int = Decimal("0"),
        entry_fee_rate: Decimal | float | int = Decimal("0"),
        entry_bid: object = None,
        entry_ask: object = None,
        exit_avg_price: Decimal | None = None,
        exit_min_profit_amount: Decimal | float | int = Decimal("0"),
        exit_allow_loss_exit: bool = False,
        exit_fee_rate: Decimal | float | int = Decimal("0"),
        exit_entry_reference_quantity: Decimal | float | int | None = None,
        final_entry_policy_check: EntryPolicyCheck | None = None,
        market: str = "US",
        reduce_only: bool = False,
    ) -> OrderStatus | ApprovedOrder:
        protective_commit_required = False
        boundary_result = self.pre_submit_risk_check(
            _PreSubmitRiskRequest(
                action=action,
                symbol=symbol,
                quantity=qty,
                price=price,
            ),
            broker,
        )
        match boundary_result:
            case OrderStatus() as rejection:
                return rejection
            case ApprovedOrder() as boundary_approval:
                final_executable_price = (
                    boundary_approval.price
                    if boundary_approval.action in _ENTRY_ACTIONS
                    else None
                )
                final_bid = boundary_approval.bid
                final_ask = boundary_approval.ask
            case unreachable:
                assert_never(unreachable)

        with self._state_lock:
            unresolved_order_ids = (
                sorted(self._pending_orders_by_id)
                if action in _ENTRY_ACTIONS
                else []
            )
            pending = self._pending_orders.get(symbol)
        if unresolved_order_ids:
            return self._skip_order(
                symbol,
                action,
                "live or unresolved broker orders appeared before submission: "
                + ", ".join(unresolved_order_ids),
                skip_category="PENDING",
            )
        if pending is not None:
            logger.warning(
                "submission skipped: pending order %s appeared for %s",
                pending.broker_order_id,
                symbol,
            )
            return self._skip_order(
                symbol,
                action,
                "pending order appeared before submission",
                skip_category="PENDING",
            )
        if (
            reduce_only
            and risk.paused
            and risk.pause_reason.startswith(_OPERATIONAL_PAUSE_PREFIXES)
        ):
            # Preserve this fact through every later broker/quote check. The
            # current pause can change before submission and must never make a
            # successfully-entered protective path skip its commit gate.
            protective_commit_required = True
            if not risk.protective_exit_permitted:
                risk_result = risk.check()
                return self._skip_order(
                    symbol,
                    action,
                    risk_result.reason,
                    skip_category="RISK",
                )
            protective_check = self._final_protective_exit_check
            if protective_check is None:
                risk.revoke_protective_exits()
                return self._skip_order(
                    symbol,
                    action,
                    "protective exit final verification is unavailable",
                    skip_category="RISK",
                )
            try:
                protective_issue = protective_check(
                    broker,
                    symbol,
                    action,
                    qty,
                    dict(self._active_execution_context),
                )
            except Exception:
                logger.exception(
                    "protective exit final verification failed for %s %s",
                    action,
                    symbol,
                )
                protective_issue = "protective exit final verification raised an exception"
            if protective_issue is not None:
                risk.revoke_protective_exits()
                return self._skip_order(
                    symbol,
                    action,
                    str(protective_issue),
                    skip_category="RISK",
                )
            if not risk.protective_exit_permitted:
                risk.revoke_protective_exits()
                return self._skip_order(
                    symbol,
                    action,
                    "protective exit permission changed during final verification",
                    skip_category="RISK",
                )

        # The account-wide protective proof must complete before this existing
        # target-symbol quantity/side gate. Both run under submission_lock.
        if action in _POSITION_REDUCING_ACTIONS:
            position_issue = self._final_reduction_position_issue(
                broker,
                symbol,
                action,
                boundary_approval.quantity,
            )
            if position_issue is not None:
                return self._pause_for_final_position_uncertainty(
                    symbol,
                    action,
                    position_issue,
                    risk,
                )
        if (
            action in _POSITION_REDUCING_ACTIONS
            and self._final_order_quote_check is not None
        ):
            try:
                quote_check_result = self._final_order_quote_check(
                    broker,
                    symbol,
                    action,
                    price,
                )
            except Exception:
                logger.exception(
                    "final quote validation failed for %s %s",
                    action,
                    symbol,
                )
                quote_check_result = "fresh executable quote could not be verified"
            if isinstance(quote_check_result, FinalOrderQuoteCheckResult):
                quote_issue = quote_check_result.issue
                final_executable_price = quote_check_result.executable_price
                final_bid = quote_check_result.bid
                final_ask = quote_check_result.ask
            else:
                quote_issue = quote_check_result
            if quote_issue:
                return self._skip_order(
                    symbol,
                    action,
                    quote_issue,
                    skip_category="RISK",
                )
        if action in _ENTRY_ACTIONS and final_entry_policy_check is not None:
            policy_rejection = self._entry_policy_rejection(
                final_entry_policy_check,
                symbol,
                action,
                market,
            )
            if policy_rejection is not None:
                return policy_rejection
        if action == "BUY" and entry_expected_exit_price is not None:
            entry_guard = self._profit_guard_for_entry(
                symbol=symbol,
                entry_price=boundary_approval.price,
                expected_exit_price=entry_expected_exit_price,
                quantity=boundary_approval.quantity,
                bid=final_bid if final_bid is not None else entry_bid,
                ask=final_ask if final_ask is not None else entry_ask,
                min_profit_amount=entry_min_profit_amount,
                fee_rate=entry_fee_rate,
            )
            if entry_guard is not None:
                return entry_guard
        if bind_final_executable_price:
            if final_executable_price is None:
                return self._skip_order(
                    symbol,
                    action,
                    "fresh executable BBO price was not bound to the reduce-only order",
                    skip_category="RISK",
                )
            marketable_price = self._normalize_marketable_limit_price(
                symbol,
                action,
                final_executable_price,
            )
            if not marketable_price.is_finite() or marketable_price <= 0:
                return self._skip_order(
                    symbol,
                    action,
                    "fresh executable BBO price is unavailable",
                    skip_category="RISK",
                )
            if (
                action in _POSITION_REDUCING_ACTIONS
                and not exit_allow_loss_exit
                and exit_avg_price is not None
            ):
                final_profit_guard = self._profit_guard_for_exit(
                    action=action,
                    symbol=symbol,
                    avg_price=exit_avg_price,
                    exit_price=marketable_price,
                    quantity=boundary_approval.quantity,
                    min_profit_amount=exit_min_profit_amount,
                    allow_loss_exit=False,
                    fee_rate=exit_fee_rate,
                    entry_reference_quantity=exit_entry_reference_quantity,
                )
                if final_profit_guard is not None:
                    return final_profit_guard
        else:
            marketable_price = None
        trading_state = risk.trading_state()
        risk_result = risk.check()
        if trading_state is TradingState.HALTED:
            reason = risk_result.reason or "trading state is HALTED"
            logger.warning(
                "submission rejected: trading state HALTED for %s %s: %s",
                action,
                symbol,
                reason,
            )
            return self._skip_order(
                symbol,
                action,
                reason,
                skip_category="RISK",
            )
        if trading_state is TradingState.REDUCING and action in _ENTRY_ACTIONS:
            reason = risk_result.reason or "trading state is REDUCING"
            logger.warning(
                "submission rejected: trading state REDUCING blocks entry %s %s: %s",
                action,
                symbol,
                reason,
            )
            return self._skip_order(
                symbol,
                action,
                reason,
                skip_category="RISK",
            )
        if not risk_result.approved and not self._risk_rejection_allows_action(
            action,
            risk,
            reduce_only=reduce_only,
        ):
            logger.warning(
                "submission rejected by final risk check for %s %s: %s",
                action,
                symbol,
                risk_result.reason,
            )
            return self._skip_order(
                symbol,
                action,
                risk_result.reason,
                skip_category="RISK",
            )
        if not risk_result.approved:
            logger.info(
                "allowing position-reducing %s despite final risk rejection: %s",
                action,
                risk_result.reason,
            )
        return dataclass_replace(
            boundary_approval,
            price=(
                marketable_price
                if marketable_price is not None
                else boundary_approval.price
            ),
            protective_commit_required=protective_commit_required,
        )

    @staticmethod
    def _final_reduction_position_issue(
        broker: BrokerGateway,
        symbol: str,
        action: str,
        requested_quantity: Decimal,
    ) -> str | None:
        expected_side = "LONG" if action == "SELL" else "SHORT"
        position_reader = getattr(broker, "get_positions", None)
        if not callable(position_reader):
            return "broker position lookup is unavailable immediately before submission"
        try:
            positions = cast("list[object]", position_reader())
        except Exception as exc:
            logger.error(
                "%s: final broker position lookup failed for %s: %s",
                action,
                symbol,
                exc,
            )
            return "broker position lookup failed immediately before submission"

        target_sides: set[str] = set()
        total_quantity = Decimal("0")
        total_available = Decimal("0")
        try:
            for position in positions:
                if str(getattr(position, "symbol", "")).upper() != symbol.upper():
                    continue
                position_quantity = Decimal(str(getattr(position, "quantity", 0)))
                if not position_quantity.is_finite() or position_quantity < 0:
                    return "broker returned an invalid target position quantity"
                if position_quantity == 0:
                    continue
                position_side = str(getattr(position, "side", "")).upper()
                target_sides.add(position_side)
                raw_available = getattr(position, "available_quantity", None)
                available_quantity = (
                    position_quantity
                    if raw_available is None
                    else Decimal(str(raw_available))
                )
                if (
                    not available_quantity.is_finite()
                    or available_quantity < 0
                    or available_quantity > position_quantity
                ):
                    return "broker returned an invalid available position quantity"
                total_quantity += position_quantity
                total_available += available_quantity
        except Exception:
            return "broker position data could not be validated immediately before submission"

        if target_sides != {expected_side}:
            actual_sides = ", ".join(sorted(target_sides)) or "FLAT"
            return (
                f"expected {expected_side} position for reduce-only {action}, "
                f"broker reported {actual_sides}"
            )
        if total_quantity < requested_quantity:
            return (
                f"broker position quantity {total_quantity} is below requested "
                f"reduce-only quantity {requested_quantity}"
            )
        if total_available < requested_quantity:
            return (
                f"broker available quantity {total_available} is below requested "
                f"reduce-only quantity {requested_quantity}"
            )
        if total_available > requested_quantity:
            # Availability grows benignly (T+ settlement, or our own cancelled
            # order releasing its reservation). Over-availability cannot cause an
            # over-sell -- the submitted quantity stays bounded by the checks
            # above -- so it must never block a firing stop. Logged, not written:
            # this runs under the submission guard on the exit hot path.
            if _REDUCTION_AVAILABILITY_LOG_THROTTLE.should_log(symbol.upper()):
                suppressed = (
                    _REDUCTION_AVAILABILITY_LOG_THROTTLE.take_suppressed_count()
                )
                logger.warning(
                    "%s: broker available quantity %s exceeds requested reduce-only "
                    "quantity %s for %s; proceeding (suppressed=%d)",
                    action,
                    total_available,
                    requested_quantity,
                    symbol,
                    suppressed,
                )
        return None

    def _pause_for_final_position_uncertainty(
        self,
        symbol: str,
        action: str,
        detail: str,
        risk: RiskController,
    ) -> OrderStatus:
        reason = (
            f"{ORDER_EXECUTION_BLOCKED_PREFIX} cannot prove reduce-only {action} "
            f"position for {symbol}: {detail}"
        )
        risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception(
                "failed to record final position validation failure for %s %s",
                action,
                symbol,
            )
        return self._skip_order(
            symbol,
            action,
            reason,
            skip_category="RISK",
        )

    def _process_submitted_order(
        self,
        approved_order: ApprovedOrder,
        result: OrderResult,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
        *,
        submit_started_at: datetime,
        submit_started_monotonic: float,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        avg_price: Decimal | None = None,
    ) -> OrderStatus | None:
        action = approved_order.action
        symbol = approved_order.symbol
        qty = approved_order.quantity
        price = approved_order.price
        acknowledged_at = datetime.now(timezone.utc)
        ack_latency_ms = (time.perf_counter() - submit_started_monotonic) * 1000
        ledger_metadata = dict(self._active_execution_context)
        ledger_metadata.update({
            "submit_started_at": submit_started_at,
            "acknowledged_at": acknowledged_at,
            "ack_latency_ms": ack_latency_ms,
            "estimated_fee": float(
                abs(qty * price * Decimal(str(ledger_metadata.get("fee_rate", 0))))
            ),
            "fee_source": "ESTIMATED",
        })
        if action in _POSITION_REDUCING_ACTIONS and avg_price is not None and avg_price > 0:
            tracked = self.tracked_position(symbol)
            expected_side = "LONG" if action == "SELL" else "SHORT"
            tracked_is_authoritative = (
                tracked is not None
                and tracked.side == expected_side
                and tracked.quantity >= qty
                and tracked.avg_price > 0
            )
            if tracked_is_authoritative:
                assert tracked is not None
                cost_basis_price = tracked.avg_price
                cost_basis_opened_at = tracked.opened_at
                position_quantity_before = tracked.quantity
            else:
                cost_basis_price = avg_price
                cost_basis_opened_at = None
                position_quantity_before = qty
            ledger_metadata.update({
                "pnl_source": (
                    "TRACKED_ENTRY" if tracked_is_authoritative else "BROKER_POSITION"
                ),
                "cost_basis_price": float(cost_basis_price),
                "cost_basis_quantity": float(qty),
                "cost_basis_opened_at": cost_basis_opened_at,
                "position_quantity_before": float(position_quantity_before),
                "pnl_fee_rate": float(
                    self._coerce_non_negative_decimal(
                        ledger_metadata.get("fee_rate", 0)
                    )
                ),
            })
        decision_at = ledger_metadata.get("decision_at")
        if isinstance(decision_at, datetime):
            ledger_metadata["submit_latency_ms"] = max(
                0.0,
                (submit_started_at - decision_at).total_seconds() * 1000,
            )
        self._active_execution_context = ledger_metadata
        status = getattr(result, "status", "SUBMITTED")
        order_status = self._order_status_from_submit_result(result)
        initial_executed_quantity = self._resolved_decimal(
            order_status,
            "executed_quantity",
            Decimal("0"),
        )
        initial_executed_price = self._resolved_decimal(
            order_status,
            "executed_price",
            Decimal("0"),
        )
        initial_execution_at = (
            datetime.now(timezone.utc)
            if str(status).upper() == "FILLED" or initial_executed_quantity > 0
            else None
        )
        try:
            self._persist_submitted_order(
                result.broker_order_id,
                symbol,
                action,
                float(qty),
                float(price),
                status,
                filled_at=initial_execution_at,
                executed_quantity=(
                    float(initial_executed_quantity)
                    if initial_executed_quantity > 0
                    else None
                ),
                executed_price=(
                    float(initial_executed_price)
                    if initial_executed_price > 0
                    else None
                ),
                ledger_metadata=ledger_metadata,
            )
        except OrderPersistenceError:
            return self._recover_from_missing_order_record(
                result,
                broker,
                risk,
                action=action,
                notifier=notifier,
                notify_risk_event=notify_risk_event,
                engine_snapshot=engine_snapshot,
                restore_engine_snapshot=restore_engine_snapshot,
                avg_price=avg_price,
            )
        if str(status).upper() == "FILLED" and order_status.actual_fee is None:
            try:
                order_status = self._coerce_order_status(
                    broker.get_order_status(result.broker_order_id),
                    result.broker_order_id,
                )
            except Exception:
                logger.warning(
                    "immediate fill %s could not be enriched with broker charges",
                    result.broker_order_id,
                    exc_info=True,
                )
        self._safe_update_order_status_from_result(order_status)

        if self._order_status_is_live(order_status):
            try:
                self._track_pending_order(
                    action,
                    result,
                    broker,
                    engine_snapshot,
                    avg_price=avg_price,
                    restore_engine_snapshot_fn=restore_engine_snapshot,
                )
            except OrderPersistenceError:
                return self._recover_from_missing_order_record(
                    result,
                    broker,
                    risk,
                    action=action,
                    notifier=notifier,
                    notify_risk_event=notify_risk_event,
                    engine_snapshot=engine_snapshot,
                    restore_engine_snapshot=restore_engine_snapshot,
                    avg_price=avg_price,
                )
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
        engine_snapshot: EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
        restore_engine_snapshot_fn: Callable[[EngineSnapshot], None] | None = None,
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
            pnl_fee_rate=self._coerce_non_negative_decimal(
                self._active_execution_context.get("fee_rate", 0)
            ),
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
            submitted_at=time.monotonic(),
            restore_engine_snapshot_fn=restore_engine_snapshot_fn,
        )
        with self._state_lock:
            existing_by_id = self._pending_orders_by_id.get(
                pending.broker_order_id
            )
            conflicting_orders = [
                existing
                for existing in self._pending_orders_by_id.values()
                if existing.broker_order_id != pending.broker_order_id
                and existing.symbol.upper() == pending.symbol.upper()
            ]
            if conflicting_orders:
                existing = conflicting_orders[0]
                raise OrderPersistenceError(
                    f"pending order {existing.broker_order_id} already tracked for {pending.symbol}; "
                    f"cannot track new order {pending.broker_order_id}"
                )
            if existing_by_id is not None:
                if existing_by_id.symbol.upper() != pending.symbol.upper():
                    raise OrderPersistenceError(
                        f"pending order {pending.broker_order_id} is already tracked for "
                        f"{existing_by_id.symbol}; cannot merge symbol {pending.symbol}"
                    )
                pending = _PendingOrder(
                    broker=pending.broker,
                    broker_order_id=pending.broker_order_id,
                    symbol=pending.symbol,
                    action=pending.action or existing_by_id.action,
                    quantity=pending.quantity,
                    price=pending.price,
                    engine_snapshot=(
                        pending.engine_snapshot
                        if pending.engine_snapshot is not None
                        else existing_by_id.engine_snapshot
                    ),
                    avg_price=(
                        pending.avg_price
                        if pending.avg_price is not None
                        else existing_by_id.avg_price
                    ),
                    pnl_fee_rate=(
                        pending.pnl_fee_rate
                        if pending.pnl_fee_rate > 0
                        else existing_by_id.pnl_fee_rate
                    ),
                    next_status_check_at=pending.next_status_check_at,
                    submitted_at=(
                        existing_by_id.submitted_at
                        if existing_by_id.submitted_at > 0
                        else pending.submitted_at
                    ),
                    restore_engine_snapshot_fn=(
                        pending.restore_engine_snapshot_fn
                        if pending.restore_engine_snapshot_fn is not None
                        else existing_by_id.restore_engine_snapshot_fn
                    ),
                    timeout_recovery_attempted=(
                        existing_by_id.timeout_recovery_attempted
                        or pending.timeout_recovery_attempted
                    ),
                )
            self._pending_orders_by_id[pending.broker_order_id] = pending
            self._rebuild_pending_orders_by_symbol_locked()

    def _clear_pending_order(self, order_id: str) -> None:
        with self._state_lock:
            removed = self._pending_orders_by_id.pop(order_id, None)
            if removed is not None:
                self._rebuild_pending_orders_by_symbol_locked()
                self._pending_status_query_warned_ids.discard(order_id)

    def _defer_pending_status_retry(self, pending: _PendingOrder, now: float) -> None:
        updated_pending = dataclass_replace(
            pending,
            next_status_check_at=now + self._order_status_poll_interval_seconds,
        )
        with self._state_lock:
            if updated_pending.broker_order_id in self._pending_orders_by_id:
                self._pending_orders_by_id[updated_pending.broker_order_id] = updated_pending
                self._rebuild_pending_orders_by_symbol_locked()

    def _reconcile_pending_order(
        self,
        pending: _PendingOrder,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        effective_restore = pending.restore_engine_snapshot_fn or restore_engine_snapshot
        now = time.monotonic()
        if now < pending.next_status_check_at:
            return
        if (
            self._order_status_timeout_seconds > 0
            and now - pending.submitted_at >= self._order_status_timeout_seconds
            and not pending.timeout_recovery_attempted
        ):
            self._handle_pending_order_timeout(
                pending,
                risk=risk,
                notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot,
                notify_risk_event=notify_risk_event,
            )
            return

        try:
            order_status = self._coerce_order_status(pending.broker.get_order_status(pending.broker_order_id), pending.broker_order_id)
        except Exception as exc:
            self._defer_pending_status_retry(pending, now)
            with self._state_lock:
                first_failure = pending.broker_order_id not in self._pending_status_query_warned_ids
                if first_failure:
                    self._pending_status_query_warned_ids.add(pending.broker_order_id)
            if first_failure:
                logger.warning(
                    "failed to query pending order status for %s; will retry after %.1fs: %s",
                    pending.broker_order_id,
                    self._order_status_poll_interval_seconds,
                    exc,
                )
            else:
                logger.debug(
                    "failed to query pending order status for %s; will retry after %.1fs",
                    pending.broker_order_id,
                    self._order_status_poll_interval_seconds,
                    exc_info=True,
                )
            return

        # Successful broker queries use the normal poll interval. Failed queries
        # are deferred in the exception path above so a transient broker detail
        # outage does not spin on every runner tick.
        updated_pending = _PendingOrder(
            broker=pending.broker,
            broker_order_id=pending.broker_order_id,
            symbol=pending.symbol,
            action=pending.action,
            quantity=pending.quantity,
            price=pending.price,
            engine_snapshot=pending.engine_snapshot,
            avg_price=pending.avg_price,
            pnl_fee_rate=pending.pnl_fee_rate,
            next_status_check_at=now + self._order_status_poll_interval_seconds,
            submitted_at=pending.submitted_at,
            restore_engine_snapshot_fn=pending.restore_engine_snapshot_fn,
            timeout_recovery_attempted=pending.timeout_recovery_attempted,
        )
        with self._state_lock:
            self._pending_orders_by_id[updated_pending.broker_order_id] = updated_pending
            self._rebuild_pending_orders_by_symbol_locked()
            self._pending_status_query_warned_ids.discard(updated_pending.broker_order_id)

        status_persisted = self._safe_update_order_status_from_result(order_status)
        status = order_status.status
        if status in {"FILLED", *_FAILED_ORDER_STATUSES} and not status_persisted:
            self._pause_for_order_status_persistence_failure(
                updated_pending,
                status,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        if status == "FILLED":
            self._finalize_pending_fill(updated_pending, order_status, risk=risk, notifier=notifier, notify_risk_event=notify_risk_event)
            self._clear_pending_order(updated_pending.broker_order_id)
            return
        if status in _FAILED_ORDER_STATUSES:
            fill_qty = self._resolved_decimal(order_status, "executed_quantity", Decimal("0"))
            if fill_qty > 0:
                self._finalize_pending_fill(updated_pending, order_status, risk=risk, notifier=notifier, fill_qty=fill_qty, notify_risk_event=notify_risk_event)
                self._clear_pending_order(updated_pending.broker_order_id)
                if self._should_restore_after_partial_terminal_fill(updated_pending, fill_qty) and effective_restore is not None and updated_pending.engine_snapshot is not None:
                    effective_restore(updated_pending.engine_snapshot)
                return
            self._pause_after_failed_order(updated_pending.broker_order_id, status, risk, notify_risk_event)
            self._clear_pending_order(updated_pending.broker_order_id)
            if effective_restore is not None and updated_pending.engine_snapshot is not None:
                effective_restore(updated_pending.engine_snapshot)
            return
        logger.debug("pending order still live: %s status=%s", updated_pending.broker_order_id, status)

    def _handle_pending_order_timeout(
        self,
        pending: _PendingOrder,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        pending = dataclass_replace(pending, timeout_recovery_attempted=True)
        with self._state_lock:
            if pending.broker_order_id in self._pending_orders_by_id:
                self._pending_orders_by_id[pending.broker_order_id] = pending
                self._rebuild_pending_orders_by_symbol_locked()
        effective_restore = pending.restore_engine_snapshot_fn or restore_engine_snapshot
        reason = (
            "ORDER_RECONCILIATION_UNCERTAIN: pending order "
            f"{pending.broker_order_id} timed out after "
            f"{self._order_status_timeout_seconds:.0f}s"
        )
        logger.warning(reason)
        try:
            order_status = self._coerce_order_status(
                pending.broker.get_order_status(pending.broker_order_id),
                pending.broker_order_id,
            )
            status_persisted = self._safe_update_order_status_from_result(order_status)
            if (
                order_status.status in {"FILLED", *_FAILED_ORDER_STATUSES}
                and not status_persisted
            ):
                self._pause_for_order_status_persistence_failure(
                    pending,
                    order_status.status,
                    risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                return
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
                    self._pause_after_timed_out_terminal_order(
                        pending.broker_order_id,
                        order_status.status,
                        reason,
                        risk,
                        notify_risk_event,
                    )
                self._clear_pending_order(pending.broker_order_id)
                if (
                    fill_qty == 0
                    or self._should_restore_after_partial_terminal_fill(pending, fill_qty)
                ) and effective_restore is not None and pending.engine_snapshot is not None:
                    effective_restore(pending.engine_snapshot)
                return
        except Exception as exc:
            logger.warning(
                "failed to query pending order status during timeout for %s: %s",
                pending.broker_order_id,
                exc,
            )

        cancel_finalized = False
        # Attempt to cancel the live order before giving up.
        try:
            cancel_result = pending.broker.cancel_order(pending.broker_order_id)
            cancel_status = self._coerce_order_status(cancel_result, pending.broker_order_id)
            cancel_persisted = self._safe_update_order_status_from_result(cancel_status)
            if (
                cancel_status.status in {"FILLED", *_FAILED_ORDER_STATUSES}
                and not cancel_persisted
            ):
                self._pause_for_order_status_persistence_failure(
                    pending,
                    cancel_status.status,
                    risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                return
            if cancel_status.status == "FILLED":
                # Order filled between status check and cancel attempt
                self._finalize_pending_fill(pending, cancel_status, risk=risk, notifier=notifier, notify_risk_event=notify_risk_event)
                self._clear_pending_order(pending.broker_order_id)
                return
            fill_qty = OrderStatus._positive(cancel_status.executed_quantity)
            if cancel_status.status in _FAILED_ORDER_STATUSES:
                # Partial fill before cancel — finalize the partial fill
                if fill_qty > 0:
                    self._finalize_pending_fill(
                        pending,
                        cancel_status,
                        risk=risk,
                        notifier=notifier,
                        fill_qty=fill_qty,
                        notify_risk_event=notify_risk_event,
                    )
                else:
                    self._pause_after_timed_out_terminal_order(
                        pending.broker_order_id,
                        cancel_status.status,
                        reason,
                        risk,
                        notify_risk_event,
                    )
                cancel_finalized = True
                self._clear_pending_order(pending.broker_order_id)
                if (
                    (fill_qty == 0 or self._should_restore_after_partial_terminal_fill(pending, fill_qty))
                    and effective_restore is not None
                    and pending.engine_snapshot is not None
                ):
                    effective_restore(pending.engine_snapshot)
                return
        except Exception as exc:
            logger.warning("failed to cancel timed-out order %s: %s", pending.broker_order_id, exc)

        if not cancel_finalized:
            try:
                recovery_status = self._coerce_order_status(
                    pending.broker.get_order_status(pending.broker_order_id),
                    pending.broker_order_id,
                )
                recovery_persisted = self._safe_update_order_status_from_result(
                    recovery_status
                )
                if (
                    recovery_status.status in {"FILLED", *_FAILED_ORDER_STATUSES}
                    and not recovery_persisted
                ):
                    self._pause_for_order_status_persistence_failure(
                        pending,
                        recovery_status.status,
                        risk=risk,
                        notify_risk_event=notify_risk_event,
                    )
                    return
                recovery_qty = self._resolved_decimal(recovery_status, "executed_quantity", Decimal("0"))
                if recovery_status.status == "FILLED" or recovery_status.status in _FAILED_ORDER_STATUSES:
                    if recovery_qty > 0:
                        self._finalize_pending_fill(
                            pending,
                            recovery_status,
                            risk=risk,
                            notifier=notifier,
                            fill_qty=recovery_qty,
                            notify_risk_event=notify_risk_event,
                        )
                    elif recovery_status.status in _FAILED_ORDER_STATUSES:
                        self._pause_after_timed_out_terminal_order(
                            pending.broker_order_id,
                            recovery_status.status,
                            reason,
                            risk,
                            notify_risk_event,
                        )
                    else:
                        # FILLED without a broker quantity uses the submitted
                        # quantity, matching the normal terminal-fill path.
                        self._finalize_pending_fill(
                            pending,
                            recovery_status,
                            risk=risk,
                            notifier=notifier,
                            notify_risk_event=notify_risk_event,
                        )
                    cancel_finalized = True
                    self._clear_pending_order(pending.broker_order_id)
                    if (
                        recovery_status.status != "FILLED"
                        and (
                            recovery_qty == 0
                            or self._should_restore_after_partial_terminal_fill(
                                pending, recovery_qty
                            )
                        )
                        and effective_restore is not None
                        and pending.engine_snapshot is not None
                    ):
                        effective_restore(pending.engine_snapshot)
                    return
            except Exception as exc:
                logger.warning(
                    "failed to recover partial fill after timeout for %s: %s",
                    pending.broker_order_id,
                    exc,
                )

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

        # Broker truth is still unknown. Keep the live order tracked and leave
        # its engine transition intact so no replacement order can be emitted.
        self._defer_pending_status_retry(pending, time.monotonic())

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
        order_id = str(pending.broker_order_id or "")
        if not order_id:
            self._finalize_pending_fill_once(
                pending,
                order_status,
                risk=risk,
                notifier=notifier,
                fill_qty=fill_qty,
                notify_risk_event=notify_risk_event,
            )
            return

        with self._state_lock:
            if order_id in self._finalized_order_ids:
                logger.debug("fill already finalized for order %s", order_id)
                return
            if order_id in self._fill_finalization_in_flight:
                logger.debug("fill finalization already in flight for order %s", order_id)
                return
            self._fill_finalization_in_flight.add(order_id)

        terminal_status = str(
            getattr(order_status, "status", "FILLED") or "FILLED"
        ).upper()
        callback_claimed = False
        try:
            if self._terminal_callback_store is not None:
                callback_claimed = self._terminal_callback_store.claim(
                    order_id,
                    terminal_status,
                )
                if not callback_claimed:
                    logger.debug(
                        "terminal callback already applied for order %s status %s",
                        order_id,
                        terminal_status,
                    )
                    return
            self._finalize_pending_fill_once(
                pending,
                order_status,
                risk=risk,
                notifier=notifier,
                fill_qty=fill_qty,
                notify_risk_event=notify_risk_event,
            )
            if self._terminal_callback_store is not None:
                self._terminal_callback_store.complete(order_id, terminal_status)
        except BaseException:
            if callback_claimed and self._terminal_callback_store is not None:
                try:
                    self._terminal_callback_store.release(order_id, terminal_status)
                except Exception:
                    logger.exception(
                        "failed to release terminal callback claim for order %s status %s",
                        order_id,
                        terminal_status,
                    )
            with self._state_lock:
                self._fill_finalization_in_flight.discard(order_id)
            raise
        else:
            with self._state_lock:
                self._fill_finalization_in_flight.discard(order_id)
                self._finalized_order_ids.add(order_id)

    def _finalize_pending_fill_once(
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
            outcome = self._persist_authoritative_exit_outcome(
                pending,
                order_status,
                fill_price=fill_price,
                fill_qty=fill_qty,
                fallback_avg_price=avg_price,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            net_pnl: Decimal | None = None
            if outcome is not None:
                gross_pnl, net_pnl = outcome
                logger.info(
                    "SELL filled: %s qty=%s price=%s avg_price=%s gross_pnl=%s net_pnl=%s",
                    pending.symbol,
                    fill_qty,
                    fill_price,
                    avg_price,
                    gross_pnl,
                    net_pnl,
                )
            else:
                logger.warning(
                    "SELL filled without authoritative tracked cost for %s; "
                    "skipping PnL recording to avoid corrupting risk state",
                    pending.symbol,
                )
            self._settle_reduction_fill(
                pending,
                fill_qty,
                net_pnl=net_pnl,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            self._safe_notify_order(
                notifier,
                "SELL",
                pending.symbol,
                str(fill_qty),
                str(fill_price),
                pending.broker_order_id,
            )
            self._mark_fill_processed(pending.symbol, pending.action)
            self._notify_reduction_fill(pending.symbol, pending.action, fill_qty)
            return
        if pending.action == "BUY_TO_COVER":
            avg_price = self._resolve_avg_price_for_exit(pending.symbol, pending.avg_price if pending.avg_price is not None and pending.avg_price > 0 else None, fill_qty)
            outcome = self._persist_authoritative_exit_outcome(
                pending,
                order_status,
                fill_price=fill_price,
                fill_qty=fill_qty,
                fallback_avg_price=avg_price,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            net_pnl = None
            if outcome is not None:
                gross_pnl, net_pnl = outcome
                logger.info(
                    "BUY_TO_COVER filled: %s qty=%s price=%s avg_price=%s gross_pnl=%s net_pnl=%s",
                    pending.symbol,
                    fill_qty,
                    fill_price,
                    avg_price,
                    gross_pnl,
                    net_pnl,
                )
            else:
                logger.warning(
                    "BUY_TO_COVER filled without authoritative tracked cost for %s; "
                    "skipping PnL recording to avoid corrupting risk state",
                    pending.symbol,
                )
            self._settle_reduction_fill(
                pending,
                fill_qty,
                net_pnl=net_pnl,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            self._safe_notify_order(
                notifier,
                "BUY_TO_COVER",
                pending.symbol,
                str(fill_qty),
                str(fill_price),
                pending.broker_order_id,
            )
            self._mark_fill_processed(pending.symbol, pending.action)
            self._notify_reduction_fill(pending.symbol, pending.action, fill_qty)
            return
        if pending.action in {"BUY", "SELL_SHORT"}:
            self._record_entry_fill(
                pending,
                fill_price,
                fill_qty,
                side="SHORT" if pending.action == "SELL_SHORT" else "LONG",
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
        self._safe_notify_order(notifier, pending.action, pending.symbol, str(fill_qty), str(fill_price), pending.broker_order_id)
        self._mark_fill_processed(pending.symbol, pending.action)
        logger.info("%s filled: %s qty=%s price=%s", pending.action, pending.symbol, fill_qty, fill_price)

    def _persist_authoritative_exit_outcome(
        self,
        pending: _PendingOrder,
        order_status: object,
        *,
        fill_price: Decimal,
        fill_qty: Decimal,
        fallback_avg_price: Decimal,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> tuple[Decimal, Decimal] | None:
        expected_side = "LONG" if pending.action == "SELL" else "SHORT"
        tracked = self.tracked_position(pending.symbol)
        tracked_is_authoritative = (
            tracked is not None
            and tracked.side == expected_side
            and tracked.quantity >= fill_qty
            and tracked.avg_price > 0
        )
        if not tracked_is_authoritative and fallback_avg_price <= 0:
            logger.error(
                "cannot persist authoritative %s outcome for %s: tracked side/quantity/cost "
                "does not cover fill; tracked=%s fill_qty=%s fallback_avg=%s",
                pending.action,
                pending.symbol,
                tracked,
                fill_qty,
                fallback_avg_price,
            )
            return None

        if tracked_is_authoritative:
            assert tracked is not None
            cost_basis_price = tracked.avg_price
            position_quantity_before = tracked.quantity
            cost_basis_opened_at = tracked.opened_at
        else:
            cost_basis_price = fallback_avg_price
            position_quantity_before = max(pending.quantity, fill_qty)
            cost_basis_opened_at = None
        pnl_source = "TRACKED_ENTRY" if tracked_is_authoritative else "BROKER_POSITION"
        gross_pnl = (
            (fill_price - cost_basis_price) * fill_qty
            if pending.action == "SELL"
            else (cost_basis_price - fill_price) * fill_qty
        )
        fee_rate = self._coerce_non_negative_decimal(pending.pnl_fee_rate)
        entry_fee = cost_basis_price * fill_qty * fee_rate
        actual_fee_raw = getattr(order_status, "actual_fee", None)
        actual_exit_fee: Decimal | None = None
        if actual_fee_raw is not None:
            try:
                candidate = Decimal(str(actual_fee_raw))
                if candidate.is_finite() and candidate >= 0:
                    actual_exit_fee = candidate
            except Exception:
                actual_exit_fee = None
        if actual_exit_fee is None:
            exit_fee = fill_price * fill_qty * fee_rate
            pnl_fee_source = "ESTIMATED"
        else:
            exit_fee = actual_exit_fee
            pnl_fee_source = "MIXED"
        pnl_fee = entry_fee + exit_fee
        net_pnl = gross_pnl - pnl_fee
        metadata: dict[str, object] = {
            "pnl_source": pnl_source,
            "cost_basis_price": float(cost_basis_price),
            "cost_basis_quantity": float(fill_qty),
            "cost_basis_opened_at": cost_basis_opened_at,
            "position_quantity_before": float(position_quantity_before),
            "gross_pnl": float(gross_pnl),
            "pnl_fee": float(pnl_fee),
            "pnl_fee_source": pnl_fee_source,
            "pnl_fee_rate": float(fee_rate),
            "net_pnl": float(net_pnl),
        }
        filled_at = getattr(order_status, "broker_updated_at", None) or datetime.now(timezone.utc)
        persisted = self._safe_update_order_status(
            pending.broker_order_id,
            str(getattr(order_status, "status", "FILLED") or "FILLED"),
            filled_at,
            float(fill_qty),
            float(fill_price),
            metadata,
        )
        if not persisted:
            self._pause_for_order_status_persistence_failure(
                pending,
                "FILLED_ACCOUNTING",
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            raise OrderPersistenceError(
                f"failed to persist authoritative accounting for order "
                f"{pending.broker_order_id}"
            )
        return gross_pnl, net_pnl

    def _settle_reduction_fill(
        self,
        pending: _PendingOrder,
        fill_qty: Decimal,
        *,
        net_pnl: Decimal | None,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        """Persist tracked-position reduction before changing in-memory state.

        Retrying a terminal fill must be idempotent. The durable tracked-entry
        update is therefore written from the unchanged in-memory snapshot,
        followed by the risk update, and only then applied in memory. If either
        durable accounting or risk settlement fails, the pending fill remains
        retryable and no quantity is consumed in memory.
        """
        if fill_qty <= 0:
            return
        try:
            with self._state_lock:
                entry = self._entry_positions.get(pending.symbol)
                consumed = Decimal("0")
                new_quantity = Decimal("0")
                new_cost = Decimal("0")
                if entry is not None and entry.quantity > 0:
                    consumed = min(fill_qty, entry.quantity)
                    avg_price = entry.avg_price
                    new_quantity = entry.quantity - consumed
                    new_cost = entry.cost - avg_price * consumed
                    if new_quantity <= 0:
                        new_quantity = Decimal("0")
                        new_cost = Decimal("0")
                    elif new_cost < 0:
                        logger.warning(
                            "cost clamp for %s: cost went negative (%s), resetting to 0",
                            pending.symbol,
                            new_cost,
                        )
                        new_cost = Decimal("0")

                    if self._persist_entry is not None:
                        try:
                            self._persist_entry(
                                pending.symbol,
                                new_quantity,
                                new_cost,
                            )
                        except Exception as exc:
                            logger.exception(
                                "failed to persist tracked reduction for %s",
                                pending.symbol,
                            )
                            raise OrderPersistenceError(
                                f"failed to persist tracked reduction for "
                                f"{pending.symbol}"
                            ) from exc

                if net_pnl is not None and risk is not None:
                    risk.record_trade(float(net_pnl))
                    drawdown_reason = risk.consume_drawdown_limit_reason()
                    if drawdown_reason is not None:
                        try:
                            self._record_risk_event(
                                drawdown_reason,
                                "DRAWDOWN_LIMIT",
                            )
                        except Exception:
                            logger.exception(
                                "failed to record drawdown limit risk event for %s",
                                pending.symbol,
                            )
                        if notify_risk_event is not None:
                            try:
                                notify_risk_event(
                                    "DRAWDOWN_LIMIT",
                                    drawdown_reason,
                                )
                            except Exception:
                                logger.exception(
                                    "failed to send drawdown limit notification for %s",
                                    pending.symbol,
                                )

                if entry is not None and consumed > 0:
                    if new_quantity <= 0:
                        self._entry_positions.pop(pending.symbol, None)
                    else:
                        entry.quantity = new_quantity
                        entry.cost = new_cost
        except Exception:
            self._pause_for_order_status_persistence_failure(
                pending,
                "FILLED_ACCOUNTING",
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            raise

    def _record_entry_fill(
        self,
        pending: _PendingOrder,
        fill_price: Decimal,
        fill_qty: Decimal,
        *,
        side: str,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        try:
            self._record_entry_price(
                pending.symbol,
                fill_price,
                fill_qty,
                side=side,
                raise_on_persistence_error=True,
            )
        except Exception:
            self._pause_for_order_status_persistence_failure(
                pending,
                "FILLED_TRACKED_ENTRY",
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            raise

    def _notify_reduction_fill(self, symbol: str, action: str, fill_qty: Decimal) -> None:
        if self._on_reduction_fill is None:
            return
        try:
            self._on_reduction_fill(symbol, action, fill_qty)
        except Exception:
            logger.exception("failed to finalize reduction fill for %s %s", action, symbol)

    def _mark_fill_processed(self, symbol: str, action: str) -> None:
        if self._on_fill is None:
            return
        try:
            self._on_fill(symbol, action)
        except Exception:
            logger.exception("failed to run fill callback")

    @staticmethod
    def _should_restore_after_partial_terminal_fill(pending: _PendingOrder, fill_qty: Decimal) -> bool:
        # A partially-filled entry owns a real position, so its transitioned
        # LONG/SHORT state must remain. A partially-filled exit leaves shares
        # behind and therefore restores the pre-submit LONG/SHORT snapshot.
        if pending.action in {"BUY", "SELL_SHORT"}:
            return False
        return pending.action in {"SELL", "BUY_TO_COVER"} and fill_qty < pending.quantity

    def _handle_terminal_fill_result(
        self,
        action: str,
        result: OrderResult,
        order_status: OrderStatus,
        broker: BrokerGateway,
        risk: RiskController | None,
        notifier: "NotifierInterface | None",
        engine_snapshot: EngineSnapshot | None,
        *,
        avg_price: Decimal | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
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
            pnl_fee_rate=self._coerce_non_negative_decimal(
                self._active_execution_context.get("fee_rate", 0)
            ),
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
        reason = f"{ORDER_EXECUTION_BLOCKED_PREFIX} order {order_id} ended with status {status}"
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

    def _pause_after_timed_out_terminal_order(
        self,
        order_id: str,
        status: str,
        timeout_reason: str,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        detail = timeout_reason.split(":", 1)[-1].strip()
        reason = (
            f"{ORDER_EXECUTION_BLOCKED_PREFIX} {detail}; "
            f"terminal status {status}"
        )
        if risk is not None:
            risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception("failed to record timed-out order %s", order_id)
        if notify_risk_event is not None:
            try:
                notify_risk_event("ORDER_TIMEOUT", reason)
            except Exception:
                logger.exception("failed to send timeout notification for %s", order_id)

    @staticmethod
    def _resolved_decimal(item: object, name: str, fallback: Decimal) -> Decimal:
        value = getattr(item, name, Decimal("0"))
        try:
            decimal_value = Decimal(str(value))
        except Exception:
            return fallback
        return decimal_value if decimal_value > 0 else fallback

    def _persist_submitted_order(
        self,
        order_id: str,
        symbol: str,
        action: str,
        qty: float,
        price: float,
        status: str = "SUBMITTED",
        *,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
        ledger_metadata: Mapping[str, object] | None = None,
    ) -> None:
        try:
            args = (
                order_id,
                symbol,
                action,
                qty,
                price,
                status,
                filled_at,
                executed_quantity,
                executed_price,
            )
            if self._accepts_positional_args(self._record_order, 10):
                self._record_order(
                    *args,
                    dict(ledger_metadata or self._active_execution_context),
                )
            else:
                self._record_order(*args)
        except Exception as exc:
            logger.exception("failed to record order %s for %s", order_id, symbol)
            raise OrderPersistenceError(f"failed to persist order {order_id}") from exc

    def _recover_from_missing_order_record(
        self,
        result: OrderResult,
        broker: BrokerGateway,
        risk: RiskController,
        *,
        action: str | None = None,
        notifier: "NotifierInterface | None" = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
        engine_snapshot: EngineSnapshot | None = None,
        restore_engine_snapshot: Callable[[EngineSnapshot], None] | None = None,
        avg_price: Decimal | None = None,
    ) -> OrderStatus:
        reason = (
            f"{ORDER_PERSISTENCE_UNCERTAIN_PREFIX} order {result.broker_order_id} "
            "submitted but local record failed"
        )
        logger.error(reason)
        resolved_action = str(action or result.side).upper()
        cancel_status: OrderStatus | None = None
        if self._order_status_is_live(result):
            try:
                cancel_status = self._coerce_order_status(
                    broker.cancel_order(result.broker_order_id),
                    result.broker_order_id,
                )
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
        pending = _PendingOrder(
            broker=broker,
            broker_order_id=result.broker_order_id,
            symbol=result.symbol,
            action=resolved_action,
            quantity=result.quantity,
            price=result.price,
            engine_snapshot=engine_snapshot,
            avg_price=avg_price,
            pnl_fee_rate=self._coerce_non_negative_decimal(
                self._active_execution_context.get("fee_rate", 0)
            ),
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
            submitted_at=time.monotonic(),
            restore_engine_snapshot_fn=restore_engine_snapshot,
        )

        if cancel_status is not None:
            fill_qty = OrderStatus._positive(cancel_status.executed_quantity)
            if cancel_status.status in {"FILLED", *_FAILED_ORDER_STATUSES}:
                if not self._ensure_recovered_terminal_order_record(
                    result,
                    resolved_action,
                    cancel_status,
                ):
                    with self._state_lock:
                        self._pending_orders_by_id.setdefault(
                            pending.broker_order_id,
                            pending,
                        )
                        self._rebuild_pending_orders_by_symbol_locked()
                    return OrderStatus(
                        result.broker_order_id,
                        "SUBMITTED",
                        reason=reason,
                    )
            if cancel_status.status == "FILLED" or (
                cancel_status.status in _FAILED_ORDER_STATUSES and fill_qty > 0
            ):
                finalized_fill_qty = fill_qty or pending.quantity
                self._finalize_pending_fill(
                    pending,
                    cancel_status,
                    risk=risk,
                    notifier=notifier,
                    fill_qty=finalized_fill_qty,
                    notify_risk_event=notify_risk_event,
                )
                if (
                    self._should_restore_after_partial_terminal_fill(
                        pending,
                        finalized_fill_qty,
                    )
                    and restore_engine_snapshot is not None
                    and engine_snapshot is not None
                ):
                    restore_engine_snapshot(engine_snapshot)
                return OrderStatus(
                    broker_order_id=cancel_status.broker_order_id,
                    status=cancel_status.status,
                    executed_quantity=cancel_status.executed_quantity,
                    executed_price=cancel_status.executed_price,
                    reason=cancel_status.reason,
                    fill_finalized=True,
                )
            if cancel_status.status in _FAILED_ORDER_STATUSES:
                if restore_engine_snapshot is not None and engine_snapshot is not None:
                    restore_engine_snapshot(engine_snapshot)
                return OrderStatus(
                    result.broker_order_id,
                    cancel_status.status,
                    reason=reason,
                )

        # No explicit terminal broker result: retain an in-memory unresolved
        # order and the transitioned engine state. The operational pause is
        # persisted by the runner and blocks automatic resubmission.
        with self._state_lock:
            self._pending_orders_by_id.setdefault(pending.broker_order_id, pending)
            self._rebuild_pending_orders_by_symbol_locked()
        return OrderStatus(result.broker_order_id, "SUBMITTED", reason=reason)

    def _ensure_recovered_terminal_order_record(
        self,
        result: OrderResult,
        action: str,
        terminal_status: OrderStatus,
    ) -> bool:
        if self._safe_update_order_status_from_result(terminal_status):
            return True
        try:
            self._persist_submitted_order(
                result.broker_order_id,
                result.symbol,
                action,
                float(result.quantity),
                float(result.price),
                "SUBMITTED",
            )
        except OrderPersistenceError:
            return False
        return self._safe_update_order_status_from_result(terminal_status)

    def _safe_update_order_status(
        self,
        order_id: str,
        status: str,
        filled_at: datetime | None = None,
        executed_quantity: float | None = None,
        executed_price: float | None = None,
        ledger_metadata: Mapping[str, object] | None = None,
    ) -> bool:
        args = (order_id, status, filled_at, executed_quantity, executed_price)
        last_attempt = _TERMINAL_STATUS_PERSIST_BACKOFF_SECONDS[-1]
        for delay in _TERMINAL_STATUS_PERSIST_BACKOFF_SECONDS:
            try:
                if self._accepts_positional_args(self._update_order_status, 6):
                    self._update_order_status(*args, dict(ledger_metadata or {}))
                else:
                    self._update_order_status(*args)
            except Exception as exc:
                # SQLite serialises writers, so a concurrent commit fails this
                # write immediately rather than waiting out `busy_timeout`.
                # Such collisions clear in milliseconds; pausing on the first
                # one blocks protective exits, so retry those specifically.
                # Any other failure is deterministic and retrying only delays
                # the fail-closed pause.
                if delay is last_attempt or not _is_transient_write_conflict(exc):
                    logger.exception(
                        "failed to update order %s to status %s",
                        order_id,
                        status,
                    )
                    return False
                time.sleep(delay)
                continue
            return True
        return False

    def _safe_update_order_status_from_result(self, result: object) -> bool:
        status = getattr(result, "status", "SUBMITTED")
        if status == "SUBMITTED":
            return True
        broker_order_id = getattr(result, "broker_order_id", None)
        executed_quantity = getattr(result, "executed_quantity", None)
        executed_price = getattr(result, "executed_price", None)
        resolved_quantity = self._resolved_decimal(
            result,
            "executed_quantity",
            Decimal("0"),
        )
        filled_at = (
            getattr(result, "broker_updated_at", None) or datetime.now(timezone.utc)
            if status == "FILLED" or resolved_quantity > 0
            else None
        )
        metadata: dict[str, object] = {}
        for name in (
            "actual_fee",
            "fee_currency",
            "broker_submitted_at",
            "broker_updated_at",
        ):
            value = getattr(result, name, None)
            if value is not None and value != "":
                metadata[name] = value
        if "actual_fee" in metadata:
            metadata["fee_source"] = "ACTUAL"
        return self._safe_update_order_status(
            broker_order_id or "",
            status,
            filled_at,
            float(executed_quantity) if executed_quantity is not None else None,
            float(executed_price) if executed_price is not None else None,
            metadata,
        )

    def _pause_for_order_status_persistence_failure(
        self,
        pending: _PendingOrder,
        status: str,
        *,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        reason = (
            f"{ORDER_STATUS_PERSISTENCE_UNCERTAIN_PREFIX} cannot persist terminal "
            f"status {status} for order {pending.broker_order_id}"
        )
        settled = dataclass_replace(pending, known_terminal_status=status)
        if settled.broker_order_id:
            with self._state_lock:
                self._pending_orders_by_id[settled.broker_order_id] = settled
                self._rebuild_pending_orders_by_symbol_locked()
        self._defer_pending_status_retry(settled, time.monotonic())
        if risk is not None:
            risk.pause(reason, auto_resumable=False)
        try:
            self._record_risk_event(reason)
        except Exception:
            logger.exception(
                "failed to record terminal-status persistence risk for %s",
                pending.broker_order_id,
            )
        if notify_risk_event is not None:
            try:
                notify_risk_event("ORDER_STATUS_PERSISTENCE_FAILED", reason)
            except Exception:
                logger.exception(
                    "failed to notify terminal-status persistence risk for %s",
                    pending.broker_order_id,
                )

    @staticmethod
    def _order_status_from_submit_result(result: OrderResult) -> OrderStatus:
        status = getattr(result, "status", "SUBMITTED")
        return OrderStatus(
            broker_order_id=result.broker_order_id,
            status=status,
            executed_quantity=getattr(result, "executed_quantity", getattr(result, "quantity", Decimal("0"))) if status == "FILLED" else Decimal("0"),
            executed_price=getattr(result, "executed_price", getattr(result, "price", Decimal("0"))) if status == "FILLED" else Decimal("0"),
            actual_fee=getattr(result, "actual_fee", None),
            fee_currency=str(getattr(result, "fee_currency", "") or ""),
            broker_submitted_at=getattr(result, "broker_submitted_at", None),
            broker_updated_at=getattr(result, "broker_updated_at", None),
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
        raw_order_id = getattr(result, "broker_order_id", None)
        broker_order_id = str(raw_order_id or "").strip()
        if not broker_order_id:
            raise ValueError("broker order status response is missing order_id")
        if broker_order_id != default_order_id:
            raise ValueError(
                "broker order status response id mismatch: "
                f"expected {default_order_id}, got {broker_order_id}"
            )
        status = getattr(result, "status", "SUBMITTED")
        # Use None (not 0) when the broker did not report a fill. The runner's
        # _update_order_status only overwrites executed_* when the new value
        # is non-None, so passing 0 would clobber a previously-recorded partial
        # fill and drop the order from daily PnL.
        raw_qty = getattr(result, "executed_quantity", None)
        raw_price = getattr(result, "executed_price", None)
        # Use None (not 0) when the broker did not report a fill. The runner's
        # _update_order_status only overwrites executed_* when the new value
        # is non-None, so passing 0 would clobber a previously-recorded partial
        # fill and drop the order from daily PnL.
        executed_qty: Decimal | None
        if raw_qty is None or raw_qty == 0:
            executed_qty = None
        else:
            executed_qty = TradeExecutionService._resolved_decimal(result, "executed_quantity", Decimal("0"))
        executed_price: Decimal | None
        if raw_price is None or raw_price == 0:
            executed_price = None
        else:
            executed_price = TradeExecutionService._resolved_decimal(result, "executed_price", Decimal("0"))
        return OrderStatus(
            broker_order_id=broker_order_id,
            status=status,
            executed_quantity=executed_qty,
            executed_price=executed_price,
            actual_fee=getattr(result, "actual_fee", None),
            fee_currency=str(getattr(result, "fee_currency", "") or ""),
            broker_submitted_at=getattr(result, "broker_submitted_at", None),
            broker_updated_at=getattr(result, "broker_updated_at", None),
        )

    def _record_entry_price(
        self,
        symbol: str,
        fill_price: Decimal,
        fill_qty: Decimal,
        *,
        side: str = "LONG",
        raise_on_persistence_error: bool = False,
    ) -> None:
        if fill_price <= 0 or fill_qty <= 0:
            return
        # Do the whole read-compute-persist-write under a single lock
        # so concurrent fills for the same symbol cannot race. The
        # previous implementation read the entry under the lock,
        # released the lock, persisted, then re-acquired the lock to
        # apply the changes — which meant a second fill landing in
        # between could overwrite the first one's quantity/cost
        # (lost update).
        with self._state_lock:
            entry = self._entry_positions.get(symbol)
            current_quantity = entry.quantity if entry is not None else Decimal("0")
            current_cost = entry.cost if entry is not None else Decimal("0")
            new_quantity = current_quantity + fill_qty
            new_cost = current_cost + fill_price * fill_qty
            previous_avg = entry.avg_price if entry is not None else Decimal("0")
            opened_at = entry.opened_at if entry is not None else datetime.now(timezone.utc)

            # Production fill finalization uses strict persistence so a failed
            # write leaves this unchanged and retryable. Non-fill callers keep
            # the historical best-effort behavior for local state setup.
            if raise_on_persistence_error and self._persist_entry is not None:
                try:
                    self._persist_entry(symbol, new_quantity, new_cost)
                except Exception as exc:
                    logger.exception("failed to persist tracked entry for %s", symbol)
                    raise OrderPersistenceError(
                        f"failed to persist tracked entry for {symbol}"
                    ) from exc
            else:
                self._persist_entry_safe(symbol, new_quantity, new_cost)

            if entry is None:
                entry = _TrackedEntry()
                self._entry_positions[symbol] = entry
            entry.quantity = new_quantity
            entry.cost = new_cost
            entry.side = str(side or "LONG").upper()
            entry.opened_at = opened_at
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
            if (
                broker_avg_price is not None
                and broker_avg_price > 0
                and abs(tracked_avg - broker_avg_price) / broker_avg_price > Decimal("0.02")
            ):
                logger.warning(
                    "avg_price mismatch for %s: tracked=%s vs broker=%s, using tracked weighted entry price for accurate pnl",
                    symbol,
                    tracked_avg,
                    broker_avg_price,
                )
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
                    logger.warning("cost clamp for %s: cost went negative (%s), resetting to 0", symbol, entry.cost)
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
