from __future__ import annotations

import logging
import inspect
import json
import secrets
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace as dataclass_replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_CEILING, ROUND_FLOOR
from threading import RLock, get_ident
from typing import TYPE_CHECKING, Callable, Final, Optional, Protocol, assert_never, cast

from app.config import (
    FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND,
    FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND,
    FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND,
    settings,
)
from app.core.accounting_fees import (
    ACCOUNTING_FEE_MODEL_US_SEC98,
    allocated_entry_fee as _accounting_allocated_entry_fee,
)
from app.core.accounting_fees import order_fee as _accounting_order_fee
from app.core.board_lot import BoardLotResolution, quantize_to_board_lot
from app.core.broker import ExtendedHoursUnsupportedError
from app.core.execution_session import (
    is_extended_closing_window,
    outside_rth_for_phase,
    resolve_execution_session,
)
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
from app.domain.fill_settlement import (
    FillFacts, RepeatVerdict, compare_repeat, plan_entry_booking,
    plan_reduction_booking, settlement_key,
)
from app.domain.passive_allocation.model import (
    PASSIVE_LANE,
    PASSIVE_SYMBOL,
    ResolvedPassivePolicy,
)
from app.domain.passive_allocation import policy as passive_policy
from app.domain.passive_allocation import protocol as passive_protocol
from app.domain.passive_allocation.policy import (
    EXECUTION_CONTEXT_CLAIM_TOKEN_KEY,
    EXECUTION_CONTEXT_LANE_KEY,
    EXECUTION_CONTEXT_SIZED_QUANTITY_KEY,
    validate_passive_entry_risk,
)

if TYPE_CHECKING:
    from app.core.audit import AuditLogger
    from app.core.broker import BrokerGateway, OrderResult, Quote
    from app.core.engine import EngineSnapshot, EngineState
    from app.core.notifiers import NotifierInterface
    from app.core.risk import RiskController

logger = logging.getLogger("auto_trade.services.trade_execution_service")

# Overnight books are one-level and wider than RTH. A long entry whose
# (ask-bid)/mid exceeds this percent is refused; exits are never blocked.
_OVERNIGHT_ENTRY_MAX_SPREAD_PCT = Decimal("0.10")
_REDUCTION_AVAILABILITY_LOG_WINDOW_SECONDS = 3600.0
_REDUCTION_AVAILABILITY_LOG_THROTTLE = RepeatedLogThrottle(
    window_seconds=_REDUCTION_AVAILABILITY_LOG_WINDOW_SECONDS,
)
_BOARD_LOT_LOG_THROTTLE = RepeatedLogThrottle(window_seconds=3600.0)


class OrderPersistenceError(RuntimeError):
    """Raised when a broker order was submitted but could not be persisted locally."""


@dataclass(frozen=True, slots=True)
class SettlementIntent:
    """Absolute accounting snapshot; the injected settler commits it atomically."""

    broker_order_id: str
    facts: FillFacts
    terminal_status: str
    filled_at: datetime
    quantity_after: Decimal
    cost_after: Decimal
    side: str
    opened_at: datetime | None
    persist_position: bool
    net_pnl: Decimal | None
    metadata: Mapping[str, float | str | datetime | None]


@dataclass(frozen=True, slots=True)
class SettlementReceipt:
    intent: SettlementIntent
    is_new: bool


class SettlementConflictError(OrderPersistenceError):
    def __init__(self, broker_order_id: str, reason: str) -> None:
        self.broker_order_id = broker_order_id
        self.reason = reason
        super().__init__(f"settlement conflict for {broker_order_id}: {reason}")


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
    outside_rth: str = ""
    skip_category: str = ""

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
    price_floor: Decimal | None = None


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
    fee_model: str = ""
    next_status_check_at: float = 0.0
    submitted_at: float = 0.0
    restore_engine_snapshot_fn: Callable[[EngineSnapshot], None] | None = None
    timeout_recovery_attempted: bool = False
    known_terminal_status: str = ""
    # Round-2 finding 3: stamped True at submit time when the order was a
    # range ENTRY sized under an EFFECTIVE funded-margin exception (the
    # execution context is cleared by then, so the verdict must ride on
    # the pending itself). Drives the bounded cancel-retry path in
    # ``_reconcile_pending_order``.
    funded_margin_entry: bool = False
    extended_hours: bool = False
    extended_hours_key: tuple[str, str, date, str] | None = None
    extended_hours_cancel_requested: bool = False
    # SPY_PASSIVE protocol: durable owner reference so later status-poll /
    # fill callbacks never depend on the (cleared) active execution
    # context. Ordinary range orders stay None.
    passive_owner_ref: str = ""


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


_PassiveRiskPolicyResolver = Callable[
    [str, str, Decimal, Decimal],
    "ResolvedPassivePolicy | str | None",
]

#: Structural type of the passive submit hook bundle (frozen signatures; see
#: ``app.domain.passive_allocation.protocol.PassiveSubmitHooks``).
_PassiveSubmitHooks = passive_protocol.PassiveSubmitHooks

# Per-execute() passive protocol state, stored in the private execution
# context (scoped to one call under the submission lock) — never service state.
_PASSIVE_OWNER_KEY = "passive_submit_owner"
# Round-2 finding 3 (P1): bounded cancel-retry cap for a pending range
# ENTRY while the funded-margin exception is effective. After the cap the
# reconcile loop stops cancelling and escalates once for manual
# intervention (the order stays tracked, the pause stays on).
_PENDING_ENTRY_CANCEL_RETRY_CAP: Final[int] = 3
# Round-2 finding 4: execution-source markers that EXCLUDE an order from
# the funded-margin exception. The runner hands an explicit TOP-LEVEL
# ``execution_initiator`` marker ("RANGE" | "OPENING_MOMENTUM" | "LLM")
# on every order context, and ``_opening_execution_ledger_context``
# additionally writes ``strategy_source=OPENING_MOMENTUM`` inside the
# serialized config_snapshot — both are checked so a context built by the
# REAL runner hand-off can never relax the caps. The nested
# ``strategy_source=INTERVAL`` marker is deliberately NOT excluding: the
# primary range lane and the (P0-shadowed, unreachable) LLM lane share
# that snapshot marker, so the explicit initiator is what distinguishes
# them. The LLM exclusion is defence in depth: ``llm_shadow_mode`` is
# hard-pinned True and ``_llm_order_execution_enabled`` is always False,
# so no LLM order can reach sizing in the first place.
_FUNDED_MARGIN_EXCLUDED_SOURCES: Final[frozenset[str]] = frozenset(
    {"OPENING_MOMENTUM", "LLM"},
)
# Explicit top-level runner hand-off marker (round-2 finding 4). The
# marker is EXECUTION-INTERNAL: the service strips it from the ledger
# metadata before any durable serialization (round-3 finding 1), so it
# never reaches ORDER_SUBMITTED payload_json, orders rows, audit logs or
# the event-list API. When the exception was EFFECTIVE, a small explicit
# evidence block is persisted instead (round-3 finding 2).
EXECUTION_CONTEXT_INITIATOR_KEY: Final[str] = "execution_initiator"
# Persisted evidence block key (ONLY written when the exception applied).
FUNDED_MARGIN_EVIDENCE_KEY: Final[str] = "funded_margin"
_PASSIVE_VALIDATED_KEY = "passive_validated_intent"
_PASSIVE_CASH_KEY = "passive_cash_evidence"
_PASSIVE_FINAL_ORDER_KEY = "passive_final_order"
_PASSIVE_SUBMIT_RIGHT_KEY = "passive_submit_right_won"
_PASSIVE_ESCALATED_KEY = "passive_receipt_escalated_uncertain"


def _passive_owner_ref_string(
    owner: passive_protocol.PassiveOwner,
) -> str:
    """Complete durable owner reference: mandate:claim:execution tokens."""
    return (
        f"{owner.ref.mandate_id}:{owner.ref.claim_token}:"
        f"{owner.execution_token}"
    )


def _passive_attempt_ref_string(
    ref: passive_protocol.PassiveAttemptRef,
) -> str:
    """Durable attempt reference (mandate:claim) for incident records."""
    return f"{ref.mandate_id}:{ref.claim_token}"


def _passive_config_snapshot_json(owner: passive_protocol.PassiveOwner) -> str:
    """Trusted config_snapshot for the passive lane (R1-5d).

    The existing ``record_order``/reload pipeline extracts the accounting
    model from the ``config_snapshot`` JSON (``model_from_config_snapshot``)
    — a root-only ``accounting_fee_model`` marker does not survive the
    reload. Build the snapshot here with the SEC98 marker, market and the
    protocol identity so pending/settlement reloads keep the trusted facts.
    """
    import json as _json

    return _json.dumps(
        {
            "strategy_source": "SPY_PASSIVE",
            "market": "US",
            "accounting_fee_model": ACCOUNTING_FEE_MODEL_US_SEC98,
            "passive_protocol_version": (
                passive_protocol.PASSIVE_PROTOCOL_VERSION
            ),
            "passive_lane": PASSIVE_LANE,
            "passive_policy_version": owner.intent.policy.policy_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )

#: Private passive protocol context keys. Caller-supplied values for these
#: keys are ALWAYS stripped at the ``execute()`` boundary (R1-3): authority
#: flows only through the trusted lifecycle entry that won the CAS steps.
_PRIVATE_PASSIVE_CONTEXT_KEYS = frozenset(
    {
        _PASSIVE_OWNER_KEY,
        _PASSIVE_VALIDATED_KEY,
        _PASSIVE_CASH_KEY,
        _PASSIVE_FINAL_ORDER_KEY,
        _PASSIVE_SUBMIT_RIGHT_KEY,
        _PASSIVE_ESCALATED_KEY,
    },
)

PASSIVE_CASH_CURRENCY_LITERAL = "USD"
_UsdCashEvidenceLike = passive_protocol.UsdCashEvidence
ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL = "ORDER_RECONCILIATION_UNCERTAIN:"
_UNCERTAIN_REASON_PREFIXES: tuple[str, ...] = (
    ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL,
    "ORDER_PERSISTENCE_UNCERTAIN:",
)


@dataclass(frozen=True, slots=True)
class _PassiveCallState:
    """In-process passive authority installed ONLY by the dedicated entry.

    Exceptional-remediation B1: the state is set, consumed and cleared
    INSIDE the same ``with self._submission_lock:`` block that performs
    the shared ``execute()`` call, and records the INSTALLING thread id —
    a generic ``execute()`` on any other thread can never borrow the
    owner/cash, and no in-flight call ever clears another's state.
    """

    owner: "passive_protocol.PassiveOwner"
    cash: _UsdCashEvidenceLike | None = None
    installing_thread_id: int = 0

    def owned_by_current_thread(self) -> bool:
        return (
            self.installing_thread_id != 0
            and self.installing_thread_id == get_ident()
        )


class _PassiveSubmitUncertain(RuntimeError):
    """The passive submission's durability is unproven (no broker call made).

    Used for submit-CAS commit failures: the broker was NOT called, but the
    database may or may not have committed — the attempt is uncertain and
    must pause + incident, never report a clean refusal.
    """


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
    outside_rth: str | None = None


@dataclass(frozen=True, slots=True)
class ExtendedHoursExitDecision:
    permitted: bool
    phase: str
    reason: str


_BoardLotResolver = Callable[[str], BoardLotResolution]
_RecordBoardLotResidual = Callable[[str, Decimal, int, Decimal], None]


@dataclass(frozen=True, slots=True)
class _BoardLotNormalization:
    quantity: Decimal
    lot_size: int | None
    lot_source: str
    residual: Decimal
    degraded: bool
    issue: str | None
    skip_category: str = "POSITION"


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

    def record_sized_quantity_positive(self) -> None: ...


class _TerminalCallbackStore(Protocol):
    def claim(self, broker_order_id: str, terminal_status: str) -> bool: ...

    def complete(self, broker_order_id: str, terminal_status: str) -> None: ...

    def release(self, broker_order_id: str, terminal_status: str) -> None: ...


class TradeExecutionService:
    _extended_hours_sdk_unsupported: bool = False

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
        *,
        board_lot_resolver: _BoardLotResolver | None = None,
        settle_fill: Callable[[SettlementIntent], SettlementReceipt] | None = None,
        record_board_lot_residual: _RecordBoardLotResidual | None = None,
        extended_hours_protective_exits_enabled: bool = False,
        paper_account_confirmed: bool = False,
        extended_hours_trading_enabled: bool = False,
        overnight_trading_enabled: bool = False,
        passive_risk_policy_resolver: _PassiveRiskPolicyResolver | None = None,
        passive_submit_hooks: _PassiveSubmitHooks | None = None,
        passive_reduction_quarantine: Callable[[str], str | None] | None = None,
        passive_uncertainty_sink: Callable[[str, str | None], None] | None = None,
        funded_margin_fingerprint_provider: Callable[[], str] | None = None,
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
        self._board_lot_resolver = board_lot_resolver
        self._record_board_lot_residual = record_board_lot_residual
        self._degraded_lot_rejections: dict[str, tuple[Decimal, date]] = {}
        self.extended_hours_protective_exits_enabled = extended_hours_protective_exits_enabled
        self.paper_account_confirmed = paper_account_confirmed
        self.extended_hours_trading_enabled = extended_hours_trading_enabled
        self.overnight_trading_enabled = overnight_trading_enabled
        self._passive_risk_policy_resolver = passive_risk_policy_resolver
        # Passive submit protocol v2: the all-or-nothing hook bundle. A
        # partially wired bundle is treated as absent — every passive marker
        # is then refused (never a silent fallback to the range path).
        self.passive_submit_hooks: _PassiveSubmitHooks | None = (
            passive_submit_hooks
            if passive_submit_hooks is not None
            and passive_protocol.passive_hooks_complete(passive_submit_hooks)
            else None
        )
        # Private passive authority channel: set only inside
        # execute_passive_entry for the duration of its own execute() call.
        self._passive_call_state: _PassiveCallState | None = None
        # Phase2a W2 guards: authoritative reduction-quarantine reader and
        # the no-I/O uncertainty sink (runner implements epoch-raise +
        # quarantine). Default None => exact legacy range behaviour.
        self._passive_reduction_quarantine = passive_reduction_quarantine
        self._passive_uncertainty_sink = passive_uncertainty_sink
        # Funded full-margin exception (P3a; default OFF). The provider is
        # injected by the runner and returns the CURRENT credential
        # fingerprint ONLY while all three credential parts are present,
        # else "". The binding is evaluated LAZILY — at sizing and again at
        # pre-submit — never frozen at startup (credentials load after
        # _configure_live_safety). Default None => never effective.
        self.funded_margin_fingerprint_provider = (
            funded_margin_fingerprint_provider
        )
        # Runtime-configurable exception knobs (runner updates these from
        # Settings; tests may arm them directly). Defaults keep the
        # exception inert and byte-for-byte identical to flag-off.
        self.funded_margin_enabled: bool = False
        self.funded_margin_account_fingerprint: str = ""
        self.funded_margin_requested_quantity: int = 0
        self.funded_margin_requested_notional: float = 0.0
        self.funded_margin_requested_risk: float = 0.0
        # RAW (pre-hard_ceiling) strategy caps handed over by the runner.
        # The clamped fields above keep their values and semantics; only
        # the exception resolver reads these.
        self.raw_strategy_max_position_quantity: int | None = None
        self.raw_strategy_max_position_notional: float | None = None
        self.raw_strategy_max_risk_per_trade: float | None = None
        self._funded_margin_last_binding_status: str = "DISABLED"
        self._funded_margin_last_limiting_factor: str | None = None
        # Round-2 finding 3: bounded cancel attempts per pending ENTRY
        # order id (funded-margin effective path only).
        self._pending_entry_cancel_attempts: dict[str, int] = {}
        # Round-3 finding 2: submit-time frozen funded-margin verdict for
        # the order currently being submitted (set by
        # _process_submitted_order, consumed by _track_pending_order and
        # the submission-record-failure recovery; reset before each
        # submit).
        self._funded_margin_applied_at_submit = False
        self._execution_session_mode: str = "ANY"
        self._extended_hours_context: (
            tuple[str, str, datetime, str] | tuple[str, str, datetime] | None
        ) = None
        self._extended_hours_unsupported: set[tuple[str, str, date, str]] = set()
        self._extended_hours_attempts: dict[tuple[str, str, date, str], int] = {}
        self._extended_hours_retry_at: dict[tuple[str, str, date, str], float] = {}
        self._extended_hours_takeover_alerted: set[tuple[str, str, date, str]] = set()
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
        self._settle_fill = settle_fill or self._settle_fill_in_process
        self._settlement_receipts: dict[str, SettlementReceipt] = {}
        self._settlement_memory_applied: set[str] = set()
        self._fill_tail_completed: set[str] = set()
        self._active_execution_context: dict[str, object] = {}

    def extended_hours_exit_decision(
        self, *, action: str, symbol: str, market: str, reduce_only: bool,
        instant: datetime | None = None,
    ) -> ExtendedHoursExitDecision:
        """Shared in-memory permission check; never performs broker I/O."""
        if not (reduce_only and action in _POSITION_REDUCING_ACTIONS):
            return ExtendedHoursExitDecision(False, "UNKNOWN", "extended hours require a reduce-only exit")
        if not (
            self.extended_hours_protective_exits_enabled
            or (
                self.extended_hours_trading_enabled
                and not self.paper_account_confirmed
            )
        ):
            return ExtendedHoursExitDecision(False, "UNKNOWN", "extended-hours protective exits are disabled")
        if self.paper_account_confirmed:
            return ExtendedHoursExitDecision(False, "UNKNOWN", "paper account does not support extended hours")
        now = instant if instant is not None else datetime.now(timezone.utc)
        session = resolve_execution_session(
            market, now, overnight_enabled=self._overnight_trading_effective(),
        )
        if not session.extended_hours_executable:
            return ExtendedHoursExitDecision(False, session.phase, session.reason)
        key = (symbol.upper(), session.phase, trade_day_for(market, now), "EXIT")
        refusal = self._extended_hours_budget_refusal(key, session.phase, kind="EXIT")
        if refusal is not None:
            return refusal
        return ExtendedHoursExitDecision(True, session.phase, session.reason)

    def _tracked_long_close(self, symbol: str, action: str) -> bool:
        """True when a SELL closes a tracked LONG and cannot open exposure."""
        if action != "SELL":
            return False
        tracked = self.tracked_position(symbol)
        return (
            tracked is not None
            and tracked.side == "LONG"
            and tracked.quantity > 0
        )

    def _extended_hours_trading_effective(self) -> bool:
        """Same predicate as the earlier session gate: on and not paper."""
        return (
            self.extended_hours_trading_enabled
            and not self.paper_account_confirmed
        )

    def _overnight_trading_effective(self) -> bool:
        """Overnight is a subset of effective extended-hours trading."""
        return (
            self._extended_hours_trading_effective()
            and self.overnight_trading_enabled
        )

    def _final_submit_session_open(
        self,
        market: str,
        *,
        symbol: str,
        trading_session_mode: str,
    ) -> bool:
        """Whether a funded entry may still pass the final session re-check.

        RTH always passes this check (the cutoff is separate). Outside RTH
        the order may proceed only when this execution already carries an
        ANY-mode extended ENTRY authorization for this symbol and the
        current phase is still executable. An approval that started in RTH
        has no such authorization, so a clock that crosses 16:00 skips
        instead of submitting a plain RTH order into POST.
        """
        now = datetime.now(timezone.utc)
        try:
            in_rth = is_trading_hours(market, now)
        except TypeError:
            in_rth = is_trading_hours(market)
        if in_rth:
            return True
        if trading_session_mode != "ANY":
            return False
        if not self._extended_hours_trading_effective():
            return False
        context = self._extended_hours_context
        if context is None or len(context) < 4 or context[3] != "ENTRY":
            return False
        if str(context[0]).upper() != symbol.upper():
            return False
        try:
            session = resolve_execution_session(
                market, now, overnight_enabled=self._overnight_trading_effective(),
            )
        except TypeError:
            session = resolve_execution_session(
                market, overnight_enabled=self._overnight_trading_effective(),
            )
        return session.extended_hours_executable

    def _entry_cutoff_active(
        self,
        market: str,
        *,
        trading_session_mode: str | None = None,
        instant: datetime | None = None,
    ) -> bool:
        """Entry-cutoff predicate.

        Legacy: within ``entry_cutoff_minutes_before_close`` of the RTH close
        while RTH is open. The extended close (20:00 ET, or 03:50 ET while
        overnight is the current span) applies ONLY when the strategy mode is
        ANY and extended trading is effective. RTH_ONLY keeps the RTH close
        even if the flag is on.
        """
        minutes = self.entry_cutoff_minutes_before_close
        mode = (
            trading_session_mode
            if trading_session_mode is not None
            else self._execution_session_mode
        )
        if mode != "ANY" or not self._extended_hours_trading_effective():
            return is_closing_window(market, minutes)
        now = instant if instant is not None else datetime.now(timezone.utc)
        return is_extended_closing_window(
            market,
            minutes,
            now,
            overnight_enabled=self._overnight_trading_effective(),
        )

    def _approval_phase(self, symbol: str) -> str:
        """Phase captured when this attempt was approved.

        No extended context means the approval happened in RTH (or the flag
        was off and this helper is not consulted).
        """
        context = self._extended_hours_context
        if (
            context is None
            or len(context) < 2
            or str(context[0]).upper() != symbol.upper()
        ):
            return "RTH"
        return str(context[1])

    def _current_execution_phase(self, market: str) -> str:
        now = datetime.now(timezone.utc)
        try:
            in_rth = is_trading_hours(market, now)
        except TypeError:
            in_rth = is_trading_hours(market)
        if in_rth:
            return "RTH"
        try:
            return resolve_execution_session(
                market, now, overnight_enabled=self._overnight_trading_effective(),
            ).phase
        except TypeError:
            return resolve_execution_session(
                market, overnight_enabled=self._overnight_trading_effective(),
            ).phase

    def _phase_mismatch(
        self, symbol: str, action: str, market: str, approved: str,
    ) -> OrderStatus | None:
        """Refuse one attempt when the phase moved after approval.

        Does not pause, consume the extended budget, or add a cooldown. The
        next evaluation re-authorizes under the new phase.
        """
        if not self._extended_hours_trading_effective():
            return None
        current = self._current_execution_phase(market)
        if approved == current:
            return None
        return self._skip_order(
            symbol,
            action,
            f"execution session changed before submission: {approved} -> {current}",
            skip_category="SESSION",
        )

    def _extended_hours_entry_decision(
        self, *, symbol: str, market: str, instant: datetime | None = None,
    ) -> ExtendedHoursExitDecision | None:
        """Extended-hours ENTRY permission; None means refuse.

        Narrow by design (owner 2026-10-03): only the dedicated
        ``extended_hours_trading_enabled`` flag (NOT the older protective-exit
        flag), never a paper-attested account, only a US PRE/POST phase, and
        only a long BUY (callers check the action before asking). Shares the
        same latch/backoff/cap state as exits so an SDK-unsupported symbol
        stops being asked in both directions.
        """
        if not (
            self.extended_hours_trading_enabled
            and not self.paper_account_confirmed
        ):
            return None
        now = instant if instant is not None else datetime.now(timezone.utc)
        session = resolve_execution_session(
            market, now, overnight_enabled=self._overnight_trading_effective(),
        )
        if not session.extended_hours_executable:
            return ExtendedHoursExitDecision(False, session.phase, session.reason)
        key = (symbol.upper(), session.phase, trade_day_for(market, now), "ENTRY")
        refusal = self._extended_hours_budget_refusal(key, session.phase, kind="ENTRY")
        if refusal is not None:
            return refusal
        return ExtendedHoursExitDecision(True, session.phase, session.reason)

    def _extended_hours_budget_refusal(
        self,
        key: tuple[str, str, date, str],
        phase: str,
        *,
        kind: str,
    ) -> ExtendedHoursExitDecision | None:
        with self._state_lock:
            if self._extended_hours_sdk_unsupported:
                return ExtendedHoursExitDecision(
                    False, phase, "SDK extended-hours execution is unsupported",
                )
            if key in self._extended_hours_unsupported:
                if kind == "EXIT":
                    return ExtendedHoursExitDecision(
                        False, phase,
                        "extended-hours exits are disabled for this symbol and phase",
                    )
                return ExtendedHoursExitDecision(
                    False, phase,
                    "extended-hours execution is unsupported for this symbol and phase",
                )
            if time.monotonic() < self._extended_hours_retry_at.get(key, 0):
                return ExtendedHoursExitDecision(
                    False, phase, "extended-hours retry backoff has not elapsed",
                )
            if self._extended_hours_attempts.get(key, 0) >= 3:
                if kind == "EXIT":
                    return ExtendedHoursExitDecision(
                        False, phase,
                        "extended-hours exits are disabled for this symbol and phase",
                    )
                return ExtendedHoursExitDecision(
                    False, phase, "extended-hours phase attempt cap reached",
                )
        return None

    def _claim_exit_takeover_alert(
        self,
        key: tuple[str, str, date, str],
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> tuple[str, str, date, Decimal, Decimal] | None:
        """Decide an EXIT takeover alert. Caller holds ``_state_lock``.

        A missing notifier does not consume the dedup key. The key is
        marked only when a send will be attempted, so a slow notify cannot
        run under this lock.
        """
        symbol, phase, day, kind = key
        if kind != "EXIT" or notify_risk_event is None:
            return None
        alert_key = (symbol, phase, day, "EXIT")
        if alert_key in self._extended_hours_takeover_alerted:
            return None
        self._extended_hours_takeover_alerted.add(alert_key)
        tracked = self._entry_positions.get(symbol)
        quantity = tracked.quantity if tracked is not None else Decimal("0")
        average = tracked.avg_price if tracked is not None else Decimal("0")
        return (symbol, phase, day, quantity, average)

    def _send_exit_takeover_alert(
        self,
        claimed: tuple[str, str, date, Decimal, Decimal],
        notify_risk_event: _NotifyRiskEvent,
    ) -> None:
        """Send outside ``_state_lock``. A failed send stays deduped."""
        symbol, phase, day, quantity, average = claimed
        reason = (
            f"{symbol} {phase} {day.isoformat()} tracked quantity {quantity} "
            f"average price {average}: automatic exits disabled for this "
            "phase — manual takeover required"
        )
        try:
            cast(Callable[..., object], notify_risk_event)(
                "EXTENDED_EXITS_DISABLED",
                reason,
                severity="CRITICAL",
            )
        except TypeError:
            try:
                notify_risk_event("EXTENDED_EXITS_DISABLED", reason)
            except Exception:
                logger.exception("extended-hours takeover alert failed")
        except Exception:
            logger.exception("extended-hours takeover alert failed")

    def _extended_hours_terminal_outcome(
        self,
        key: tuple[str, str, date, str],
        *,
        unsupported: bool,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        """Bound retries for one kind. A real-unsupported signal disables both.

        Budgets, unsupported latches and takeover-alert dedup live only in
        process memory. A cold start does not restore them. A pending order
        rebuilt from the database has no extended-hours key, so it cannot
        resume the extended budget and fails closed to the ordinary path.
        """
        symbol, phase, day, kind = key
        claimed: tuple[str, str, date, Decimal, Decimal] | None = None
        write_risk_event = False
        with self._state_lock:
            if unsupported:
                for disabled in ("ENTRY", "EXIT"):
                    both = (symbol, phase, day, disabled)
                    self._extended_hours_unsupported.add(both)
                claimed = self._claim_exit_takeover_alert(
                    (symbol, phase, day, "EXIT"),
                    notify_risk_event,
                )
            else:
                attempts = self._extended_hours_attempts.get(key, 0) + 1
                self._extended_hours_attempts[key] = attempts
                self._extended_hours_retry_at[key] = time.monotonic() + 60.0
                if attempts >= 3:
                    self._extended_hours_unsupported.add(key)
                    if kind == "EXIT":
                        write_risk_event = True
                        claimed = self._claim_exit_takeover_alert(
                            key,
                            notify_risk_event,
                        )
        if claimed is not None and notify_risk_event is not None:
            threading.Thread(
                target=self._send_exit_takeover_alert,
                args=(claimed, notify_risk_event),
                name="extended-exit-takeover-alert",
                daemon=True,
            ).start()
        if write_risk_event:
            try:
                self._record_risk_event(
                    "extended-hours exits are disabled for "
                    f"{symbol} {phase}; operator takeover required"
                )
            except Exception:
                logger.exception(
                    "extended-hours exit-disable risk event failed"
                )

    def _active_extended_hours_key(self) -> tuple[str, str, date, str] | None:
        context = self._extended_hours_context
        if context is None:
            return None
        if len(context) == 3:
            symbol, phase, decided_at = context
            kind = "EXIT"
        else:
            symbol, phase, decided_at, kind = context
        return (
            symbol.upper(),
            phase,
            trade_day_for(market_for_symbol(symbol), decided_at),
            kind,
        )

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
                    fee_model=pending.fee_model,
                    next_status_check_at=pending.next_status_check_at,
                    submitted_at=pending.submitted_at,
                    restore_engine_snapshot_fn=pending.restore_engine_snapshot_fn,
                    timeout_recovery_attempted=pending.timeout_recovery_attempted,
                    extended_hours=pending.extended_hours,
                    extended_hours_key=pending.extended_hours_key,
                    extended_hours_cancel_requested=pending.extended_hours_cancel_requested,
                    passive_owner_ref=pending.passive_owner_ref,
                    funded_margin_entry=pending.funded_margin_entry,
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
                        fee_model=(
                            pending.fee_model
                            if pending.fee_model
                            else existing.fee_model
                        ),
                        # R1-5: the complete passive owner reference must
                        # survive every rebuild — prefer the existing
                        # in-memory ref, else whatever the loader carried.
                        passive_owner_ref=(
                            existing.passive_owner_ref
                            or pending.passive_owner_ref
                        ),
                        next_status_check_at=existing.next_status_check_at,
                        submitted_at=existing.submitted_at,
                        restore_engine_snapshot_fn=existing.restore_engine_snapshot_fn if existing.restore_engine_snapshot_fn is not None else pending.restore_engine_snapshot_fn,
                        timeout_recovery_attempted=existing.timeout_recovery_attempted,
                        extended_hours=existing.extended_hours or pending.extended_hours,
                        extended_hours_key=existing.extended_hours_key or pending.extended_hours_key,
                        extended_hours_cancel_requested=existing.extended_hours_cancel_requested,
                        funded_margin_entry=(
                            existing.funded_margin_entry
                            or pending.funded_margin_entry
                        ),
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

    def attach_passive_owner_ref(self, broker_order_id: str, ref: str) -> bool:
        """Attach a validated passive owner ref to an existing pending order.

        Phase2a review1 M1: the durable ``mandate:claim:exec`` reference is
        installed on a REAL pending order only — a missing pending, an
        invalid/empty ref, or an existing DIFFERENT ref (never replaced)
        all return False so the caller raises a representation issue +
        external block instead of silently continuing. Short state lock
        only; no DB/network; the pending's immutable data (id/symbol/
        action/quantity/price/snapshots) is preserved verbatim and both
        pending indexes (by id, by symbol/legacy) are rebuilt through the
        existing helpers.
        """
        broker_order_id = str(broker_order_id or "").strip()
        ref = str(ref or "").strip()
        if not broker_order_id or not ref:
            return False
        parts = ref.split(":")
        if len(parts) != 3 or not all(parts):
            return False
        with self._state_lock:
            pending = self._pending_orders_by_id.get(broker_order_id)
            if pending is None:
                return False
            existing = str(pending.passive_owner_ref or "")
            if existing:
                return existing == ref
            updated = dataclass_replace(
                pending,
                passive_owner_ref=ref,
            )
            self._pending_orders_by_id[broker_order_id] = updated
            self._rebuild_pending_orders_by_symbol_locked()
            return True

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
        extended_take_profit: bool = False,
        execution_context: Mapping[str, object] | None = None,
        is_funnel_primary: bool = False,
        entry_policy_check: EntryPolicyCheck | None = None,
        allow_opening_warmup_entry: bool = False,
        sized_quantity: Decimal | None = None,
    ) -> OrderStatus | None:
        with self._submission_lock:
            # R1-3 / final-remediation finding 1: the execution context is
            # AUDIT-ONLY, never authority — and there is NO kwargs channel
            # for passive protocol objects either. Private passive keys in
            # a caller-supplied context are stripped and REFUSE the order.
            # The ONLY way passive authority enters this service is the
            # dedicated ``execute_passive_entry`` lifecycle, which installs
            # its private state directly (``_passive_call_state``), never
            # through anything a generic caller can reach.
            _caller_context = dict(execution_context or {})
            _smuggled_private_keys = sorted(
                key for key in _caller_context
                if key in _PRIVATE_PASSIVE_CONTEXT_KEYS
            )
            self._active_execution_context = {
                key: value
                for key, value in _caller_context.items()
                if key not in _PRIVATE_PASSIVE_CONTEXT_KEYS
            }
            if _smuggled_private_keys:
                self._active_execution_context = {}
                self._extended_hours_context = None
                return self._skip_order(
                    symbol,
                    action,
                    "execution context carried private passive protocol "
                    f"keys ({', '.join(_smuggled_private_keys)}); orders "
                    "smuggling protocol authority are denied",
                    skip_category="RISK",
                )
            _passive_call = self._passive_call_state
            if _passive_call is not None and _passive_call.owned_by_current_thread():
                # Installed ONLY by the trusted lifecycle entry, in-process,
                # ON THE SAME THREAD, for the duration of its own execute()
                # call inside the same submission RLock (B1: a generic call
                # on any other thread must never borrow passive authority).
                self._active_execution_context[_PASSIVE_OWNER_KEY] = (
                    _passive_call.owner
                )
                if _passive_call.cash is not None:
                    self._active_execution_context[_PASSIVE_CASH_KEY] = (
                        _passive_call.cash
                    )
            # SPY_PASSIVE protocol: force-overwrite the trusted accounting
            # metadata for a passive entry (contract: execution layer owns
            # these; context is audit-only, not authority). Range orders
            # keep the setdefault behaviour below, unchanged.
            _passive_owner_ctx = self._active_execution_context.get(
                _PASSIVE_OWNER_KEY,
            )
            if isinstance(_passive_owner_ctx, passive_protocol.PassiveOwner):
                self._active_execution_context["market"] = "US"
                self._active_execution_context["accounting_fee_model"] = (
                    ACCOUNTING_FEE_MODEL_US_SEC98
                )
            self._active_execution_context.setdefault("market", market)
            self._active_execution_context.setdefault("fee_rate", float(fee_rate))
            self._execution_session_mode = trading_session_mode
            if sized_quantity is not None:
                self._active_execution_context[
                    EXECUTION_CONTEXT_SIZED_QUANTITY_KEY
                ] = float(sized_quantity)
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
                    extended_take_profit=extended_take_profit,
                    is_funnel_primary=is_funnel_primary,
                    entry_policy_check=entry_policy_check,
                    allow_opening_warmup_entry=(
                        allow_opening_warmup_entry
                    ),
                    sized_quantity=sized_quantity,
                )
            finally:
                self._active_execution_context = {}
                self._extended_hours_context = None

    def _passive_now(self) -> datetime:
        hooks = self._passive_hooks_or_none()
        if hooks is not None:
            return hooks.now()
        return datetime.now(timezone.utc)

    def execute_passive_entry(
        self,
        *,
        ref: passive_protocol.PassiveAttemptRef,
        quote: Quote,
        broker: BrokerGateway,
        risk: RiskController,
        notifier: "NotifierInterface",
    ) -> OrderStatus | None:
        """Execute one reserved passive intent through the shared entry path.

        Final-remediation finding 2: THIS method owns the complete passive
        lifecycle — no facade supplement, no caller-supplied owner or cash:

        1. validate the ref and win ``begin_execution`` ONCE with a fresh
           execution token (a losing/invalid ref performs ZERO owned
           outcome writes and simply returns the refusal);
        2. capture the strict USD cash evidence OUTSIDE the submission and
           state locks (a capture failure is an owned pre-broker denial:
           NO_SUBMIT, or UNCERTAIN + pause if the recording fails);
        3. drive the shared ``execute()`` entry under the submission lock —
           same single ``pre_submit_risk_check`` boundary and the same sole
           broker mutation — with the intent quantity used verbatim, never
           margin sizing.

        EVERY return path finalizes the mandate: owned definite pre-broker
        refusal -> NO_SUBMIT (a recording failure escalates to UNCERTAIN
        with a real non-auto pause + incident); unknown durable state or
        any post-broker error -> UNCERTAIN with the real risk/notifier
        collaborators, the known broker id preserved.
        """
        if not isinstance(ref, passive_protocol.PassiveAttemptRef):
            return OrderStatus(
                "",
                "UNCERTAIN",
                reason=(
                    "execute_passive_entry requires a PassiveAttemptRef; "
                    f"got {type(ref).__name__}"
                ),
            )
        ref_issue = ref.validate()
        if ref_issue is not None:
            return OrderStatus("", "SKIPPED", reason=ref_issue)
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            # Incomplete hook wiring is a configuration failure. This call
            # has won NO ownership (begin_execution never ran), so per the
            # zero-write rule it performs no mandate writes; the explicit
            # refusal names the condition. (Reservation itself refuses to
            # run without complete hooks — the service-layer gate.)
            return OrderStatus(
                "",
                "SKIPPED",
                reason=(
                    "passive submit hooks are not fully wired; refusing "
                    "the passive entry"
                ),
            )
        # Contract §Execution order: the lane gate is re-checked AFTER
        # execution ownership is won (a revoked flag/PAPER gate is a
        # durable condition that permanently consumes the intent — the
        # burn happens through the owned NO_SUBMIT write, never as a
        # no-write skip that would leave the reservation replayable).
        execution_token = secrets.token_hex(16)
        try:
            owner_result = hooks.begin_execution(ref, execution_token)
        except Exception as exc:
            # B2: ownership durability unproven. NO fabricated owner — a
            # fabricated intent cannot even be constructed for an unknown
            # reservation (zero quantity raises before any pause). The
            # real behaviour is ownerless: pause with the
            # ORDER_RECONCILIATION_UNCERTAIN prefix (non-auto), record an
            # unresolved-reference incident with the REAL ref string, and
            # return an explicit UNCERTAIN status. The row keeps whatever
            # durable state the hook committed (e.g. CHECKING with its own
            # token): never replayed, never a normal success/refusal.
            return self._escalate_ownerless_passive_uncertain(
                reference=_passive_attempt_ref_string(ref),
                issue=f"begin_execution raised {type(exc).__name__}: {exc}",
                broker_order_id=None,
                risk=risk,
                notifier=notifier,
            )
        if isinstance(owner_result, passive_protocol.PassiveRejection):
            # Lost the race or invalid row: ZERO owned writes (R1-1).
            return OrderStatus("", "SKIPPED", reason=owner_result.reason)
        owner = owner_result

        # Contract §Execution order: the lane gate binds INSIDE the owned
        # lifecycle. A flag/PAPER revocation since reservation is a durable
        # condition whose denial permanently consumes the intent (owned
        # NO_SUBMIT; a recording failure escalates to UNCERTAIN + pause).
        gate_issue = hooks.current_gate_issue()
        if gate_issue is not None:
            return self._owned_denial_or_uncertain(
                owner, gate_issue, risk, notifier,
            )

        # Strict cash evidence: captured OUTSIDE the submission and state
        # locks (contract §Cash API), then validated at the boundary AND
        # revalidated after the submit-right CAS.
        cash_snapshot: passive_protocol.UsdCashEvidence | None = None
        cash_issue = ""
        reader = getattr(broker, "get_strict_usd_cash_snapshot", None)
        if not callable(reader):
            cash_issue = (
                "strict USD cash evidence is unavailable on this broker "
                "gateway; passive entry denied"
            )
        else:
            try:
                captured = reader()
            except Exception as exc:
                cash_issue = (
                    f"strict USD cash evidence request failed "
                    f"({type(exc).__name__}); passive entry denied"
                )
            else:
                if isinstance(captured, passive_protocol.UsdCashEvidence):
                    cash_snapshot = captured
                else:
                    cash_issue = (
                        "strict USD cash evidence returned an unusable "
                        "value; passive entry denied"
                    )
        if cash_snapshot is None:
            return self._owned_denial_or_uncertain(owner, cash_issue, risk, notifier)

        context: dict[str, object] = {
            EXECUTION_CONTEXT_LANE_KEY: PASSIVE_LANE,
            EXECUTION_CONTEXT_CLAIM_TOKEN_KEY: owner.ref.claim_token,
            EXECUTION_CONTEXT_SIZED_QUANTITY_KEY: float(owner.intent.quantity),
            "market": "US",
            "accounting_fee_model": ACCOUNTING_FEE_MODEL_US_SEC98,
            "fee_rate": 0.0,
            "passive_lane_policy_version": owner.intent.policy.policy_version,
        }
        # B1: install, call and clear the private call state INSIDE the
        # same submission RLock that runs the shared execute(), tagged
        # with the installing thread id. A generic execute() on any other
        # thread cannot borrow the owner/cash, and the finally-clear only
        # ever removes OUR state (never another in-flight call's).
        call_state = _PassiveCallState(
            owner=owner,
            cash=cash_snapshot,
            installing_thread_id=get_ident(),
        )
        with self._submission_lock:
            self._passive_call_state = call_state
            try:
                status = self.execute(
                    "BUY",
                    owner.intent.symbol,
                    quote,
                    broker,
                    risk,
                    notifier,
                    PASSIVE_CASH_CURRENCY_LITERAL,
                    market="US",
                    execution_context=context,
                    sized_quantity=owner.intent.quantity,
                )
            except _PassiveSubmitUncertain as exc:
                return self._owned_uncertain_status(
                    owner, "", str(exc), risk, notifier,
                )
            except BrokerSubmissionUncertainError as exc:
                # Lost ACK after entering the broker call: possibly
                # submitted. Do NOT parse the message to assume otherwise.
                return self._escalate_passive_uncertain(
                    owner, "",
                    f"broker submit raised {type(exc).__name__}: {exc}",
                    risk=risk,
                    notifier=notifier,
                )
            finally:
                if self._passive_call_state is call_state:
                    self._passive_call_state = None
        return self._finalize_passive_execute_status(
            owner, status, risk, notifier,
        )

    def _finalize_passive_execute_status(
        self,
        owner: passive_protocol.PassiveOwner,
        status: OrderStatus | None,
        risk: RiskController,
        notifier: "NotifierInterface",
    ) -> OrderStatus | None:
        """Finalize the mandate for EVERY dedicated-entry outcome.

        Finding 2: the dedicated entry owns the complete lifecycle — a
        facade must never supplement this. ``None`` or an unclassifiable
        status is possibly-submitted (UNCERTAIN); a certain pre-broker
        refusal (SKIPPED with no broker id) is NO_SUBMIT — with a recording
        failure escalated to UNCERTAIN; a submitted-like receipt binds
        ORDER_KNOWN (an UNSETTLED recording failure is UNCERTAIN with the
        id preserved).
        """
        if status is None:
            return self._escalate_passive_uncertain(
                owner, "",
                "execution returned no classifiable status",
                risk=risk, notifier=notifier,
            )
        broker_id = str(status.broker_order_id or "")
        status_text = str(status.status or "")
        reason_text = str(status.reason or "")
        if status_text == "SKIPPED" and not broker_id:
            return self._owned_denial_or_uncertain(
                owner, reason_text or "skipped", risk, notifier,
            )
        if status_text == "UNCERTAIN" or reason_text.startswith(
            _UNCERTAIN_REASON_PREFIXES,
        ):
            return self._escalate_passive_uncertain(
                owner,
                broker_id,
                reason_text or f"unclassified status {status_text!r}",
                risk=risk,
                notifier=notifier,
            )
        classification = passive_protocol.classify_submit_receipt(
            broker_order_id=broker_id,
            status=status_text,
        )
        if classification == passive_protocol.SUBMIT_STATE_UNCERTAIN:
            return self._escalate_passive_uncertain(
                owner,
                broker_id,
                reason_text or f"unrecognized broker status {status_text!r}",
                risk=risk,
                notifier=notifier,
            )
        # The submit receipt was already bound INSIDE execute() (see
        # _record_passive_receipt, which owns the broker receipt). A
        # submitted-like terminal status carries no new fact to record;
        # re-writing it would be a same-state duplicate write, which the
        # monotonic outcome protocol correctly refuses — so the durable
        # outcome is proven by that earlier stage. Only the exception
        # paths above (and the facade's status mapping) remain.
        return status

    def _owned_denial_or_uncertain(
        self,
        owner: passive_protocol.PassiveOwner,
        reason: str,
        risk: RiskController,
        notifier: "NotifierInterface",
    ) -> OrderStatus:
        """Owned definite pre-broker refusal: NO_SUBMIT, or UNCERTAIN.

        The recording failure must NOT be swallowed (final-remediation
        finding 2): if the durable NO_SUBMIT cannot be written the attempt
        is uncertain — real risk pause + CRITICAL notification + incident.
        """
        hooks = self._passive_hooks_or_none()
        if hooks is not None:
            try:
                hooks.record_outcome(
                    owner,
                    passive_protocol.PassiveOutcomeFact(
                        outcome=passive_protocol.SUBMIT_STATE_NO_SUBMIT,
                        reason=reason,
                    ),
                )
                return OrderStatus("", "SKIPPED", reason=reason)
            except Exception as exc:
                return self._escalate_passive_uncertain(
                    owner,
                    "",
                    f"recording the no-submit denial failed: {exc}",
                    risk=risk,
                    notifier=notifier,
                )
        return self._escalate_passive_uncertain(
            owner, "",
            f"no-submit denial could not be recorded (hooks missing): {reason}",
            risk=risk, notifier=notifier,
        )

    def _owned_uncertain_status(
        self,
        owner: passive_protocol.PassiveOwner,
        broker_order_id: str,
        issue: str,
        risk: RiskController,
        notifier: "NotifierInterface",
    ) -> OrderStatus:
        return self._escalate_passive_uncertain(
            owner, broker_order_id, issue, risk=risk, notifier=notifier,
        )

    def _claim_passive_submission_right(
        self,
        approved_order: ApprovedOrder,
    ) -> str | None:
        """Consume the one-time submit right (CHECKING -> SUBMITTING).

        Called exactly once per attempt, after every runtime/policy check
        has passed and immediately before the broker call. Verifies both
        owner tokens + ACTIVE status + unchanged immutable intent, and
        persists the final order/cash/fee snapshot atomically with the
        transition. Returns None on success, or a burn reason that the
        caller turns into a NO_SUBMIT refusal. A commit failure raises
        ``_PassiveSubmitUncertain`` (no broker call happened, but durability
        is unproven — pause + incident).
        """
        owner = self._active_passive_owner()
        if owner is None:
            return "no passive owner stands behind this submission"
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            return "passive submit hooks are unavailable or incomplete"
        cash = self._passive_cash_or_none()
        if cash is None:
            return "strict cash evidence is missing for the submit right"
        final_order = passive_protocol.PassiveOrderSpec(
            symbol=approved_order.symbol,
            side=approved_order.side,
            quantity=approved_order.quantity,
            price=approved_order.price,
        )
        # R1-6: the FINAL approved price legitimately differs from the
        # original request price (boundary repricing); symbol/side/quantity
        # stay immutable, the price is revalidated against cash/allotment.
        drift = owner.intent.matches_final_order(final_order)
        if drift is not None:
            return drift
        fee = self._passive_commission_for_request(
            price=approved_order.price,
            quantity=approved_order.quantity,
        )
        cash_issue = passive_protocol.validate_cash_evidence(
            cash=cash,
            quantity=approved_order.quantity,
            approved_price=approved_order.price,
            fee=fee,
            now=self._passive_now(),
        )
        if cash_issue is not None:
            return f"strict cash: {cash_issue}"
        try:
            won = hooks.claim_submission(owner, final_order, cash)
        except Exception as exc:
            raise _PassiveSubmitUncertain(
                f"submit-right CAS failed durably: {type(exc).__name__}",
            ) from exc
        if not won:
            return (
                "the one-time submit right was consumed by a concurrent "
                "attempt or the authorisation changed"
            )
        self._active_execution_context[_PASSIVE_SUBMIT_RIGHT_KEY] = True
        self._active_execution_context[_PASSIVE_FINAL_ORDER_KEY] = final_order
        return None

    def _recheck_passive_before_broker_call(
        self,
        approved_order: ApprovedOrder,
        risk: RiskController,
    ) -> str | None:
        """Post-CAS recheck: freshness, current flag, risk — no network.

        Runs after the submit CAS's DB latency and immediately before the
        broker mutation. R1-2: a passive BUY here requires the FULL
        ``risk.check().approved`` AND an ACTIVE trading state — a manual
        pause or REDUCING that arrived during the CAS latency refuses the
        submission (the authorisation is already consumed, burned
        NO_SUBMIT). There is no retry and no network refresh. Returns None
        to proceed.
        """
        owner = self._active_passive_owner()
        if owner is None:
            return "no passive owner stands behind this submission"
        fee = self._passive_commission_for_request(
            price=approved_order.price,
            quantity=approved_order.quantity,
        )
        cash_issue = passive_protocol.validate_cash_evidence(
            cash=self._passive_cash_or_none(),
            quantity=approved_order.quantity,
            approved_price=approved_order.price,
            fee=fee,
            now=self._passive_now(),
        )
        if cash_issue is not None:
            return f"strict cash recheck: {cash_issue}"
        hooks = self._passive_hooks_or_none()
        if hooks is not None:
            gate_issue = hooks.current_gate_issue()
            if gate_issue is not None:
                return gate_issue
        if risk.kill_switch:
            return "risk state changed before the broker call: kill switch"
        trading_state = risk.trading_state()
        if trading_state is not TradingState.ACTIVE:
            return (
                "risk state changed before the broker call: trading state "
                f"is {trading_state.value}"
                + (" and paused" if risk.paused else "")
            )
        risk_result = risk.check()
        if not risk_result.approved:
            return (
                "risk state changed before the broker call: "
                f"{risk_result.reason}"
            )
        return None

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
        extended_take_profit: bool = False,
        entry_policy_check: EntryPolicyCheck | None = None,
        allow_opening_warmup_entry: bool = False,
        is_funnel_primary: bool = False,
        sized_quantity: Decimal | None = None,
    ) -> OrderStatus | None:
        decided_at = datetime.now(timezone.utc)
        try:
            outside_rth_now = not is_trading_hours(market, decided_at)
        except TypeError:
            outside_rth_now = not is_trading_hours(market)
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
            and outside_rth_now
        ):
            # Extended-hours trading opt-in (owner 2026-10-03): a long BUY in
            # an executable US PRE/POST phase may pass when the flag is
            # effective (on + not paper). Every other non-RTH case — HK,
            # overnight, weekends, holidays, half-day post, RTH_ONLY mode,
            # paper accounts, flag off — keeps today's refusal.
            extended_entry_decision = self._extended_hours_entry_decision(
                symbol=symbol, market=market,
            )
            if extended_entry_decision is None:
                return self._skip_order(
                    symbol,
                    action,
                    f"non-trading hours for {market}; ANY mode cannot open a long position",
                    skip_category="SESSION",
                )
            if not extended_entry_decision.permitted:
                return self._skip_order(
                    symbol,
                    action,
                    f"non-trading hours for {market}: {extended_entry_decision.reason}",
                    skip_category="SESSION",
                )
            self._extended_hours_context = (
                symbol,
                extended_entry_decision.phase,
                decided_at,
                "ENTRY",
            )
        if (
            trading_session_mode == "ANY"
            and self._extended_hours_trading_effective()
            and action in _POSITION_REDUCING_ACTIONS
            and outside_rth_now
            and (
                reduce_only
                or (
                    extended_take_profit
                    and self._tracked_long_close(symbol, action)
                )
            )
        ):
            # Extended-hours trading opt-in (owner 2026-10-03): with the flag
            # effective (on + not paper), an ANY-mode non-RTH reduce-only exit
            # goes through the same extended-hours decision as under RTH_ONLY,
            # so it carries outside_rth=ANY_TIME in an executable phase
            # instead of resting unfilled until the 30s status timeout. In an
            # UNAVAILABLE phase the intent is held without submitting or
            # pausing — identical outcome to RTH_ONLY. On a paper-attested
            # account the flag is ineffective (CONTRACT B/E): the block does
            # not gate at all and the exit proceeds exactly as with the flag
            # off. Flag off: same — no gate, today's behaviour.
            exit_decision = self.extended_hours_exit_decision(
                action=action, symbol=symbol, market=market,
                reduce_only=True,
            )
            if not exit_decision.permitted:
                return self._skip_order(
                    symbol,
                    action,
                    f"non-RTH for {market}: {exit_decision.reason}",
                    skip_category="SESSION",
                )
            self._extended_hours_context = (
                symbol,
                exit_decision.phase,
                decided_at,
                "EXIT",
            )
        if trading_session_mode == "RTH_ONLY":
            if outside_rth_now:
                # SESSION skip records ORDER_SKIPPED only; TRADING_SESSION_BLOCKED is layer A.
                decision = self.extended_hours_exit_decision(
                    action=action, symbol=symbol, market=market,
                    reduce_only=reduce_only, instant=decided_at,
                )
                if not decision.permitted:
                    return self._skip_order(
                        symbol, action, f"non-RTH for {market}: {decision.reason}",
                        skip_category="SESSION",
                    )
                self._extended_hours_context = (
                    symbol, decision.phase, decided_at, "EXIT",
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
        if action in _ENTRY_ACTIONS and self._entry_cutoff_active(
            market, trading_session_mode=trading_session_mode,
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
            # R1-3: ANY passive marker must be validated BEFORE sizing —
            # an unknown lane (or a sized quantity without an owner) is
            # refused before any margin read.
            lane_marker = str(
                self._active_execution_context.get(
                    EXECUTION_CONTEXT_LANE_KEY, "",
                ) or "",
            )
            if lane_marker and lane_marker != PASSIVE_LANE:
                return self._skip_order(
                    symbol,
                    action,
                    (
                        f"execution context carries unknown lane "
                        f"{lane_marker!r}; only the {PASSIVE_LANE} lane is "
                        "recognised, and a lane context must never fall "
                        "back to the range path"
                    ),
                    skip_category="RISK",
                )
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
            # sized_quantity is the passive lane's channel; without a valid
            # passive owner the request is malformed, never margin-sized.
            # (With a lane marker present the boundary itself reports the
            # missing owner — both are refusals, neither submits.)
            if (
                self._sized_entry_quantity() is not None
                and self._active_passive_owner() is None
                and not self._active_execution_context.get(
                    EXECUTION_CONTEXT_LANE_KEY,
                )
            ):
                return self._skip_order(
                    symbol,
                    action,
                    "sized_quantity is only accepted for an order bound "
                    "to a valid SPY_PASSIVE mandate authorisation; "
                    "range entries must size from buying power",
                    skip_category="RISK",
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
                is_funnel_primary=is_funnel_primary,
                sized_quantity=sized_quantity,
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
                reduce_only=reduce_only or extended_take_profit,
                extended_take_profit=extended_take_profit,
                is_funnel_primary=is_funnel_primary,
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
                is_funnel_primary=is_funnel_primary,
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
                is_funnel_primary=is_funnel_primary,
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

    def _range_entry_limits_for(
        self,
        symbol: str,
        action: str,
        market: str | None = None,
    ) -> _EntryRiskLimits | None:
        """Resolve the funded-margin exception caps for the range-lane BUY.

        Returns ``None`` unless EVERY gate holds (contract §C): the
        exception is configured (enabled + not paper + valid fingerprint +
        all three requests > 0), the CURRENT credential fingerprint
        matches (re-evaluated on every call so a rotation fails closed),
        and the order is the primary range-lane path — no passive owner or
        lane marker in the execution context, not an opening-momentum
        entry, not SELL_SHORT, not HK. Any miss returns None and the
        caller keeps ``_entry_risk_limits`` (the clamped caps) unchanged.
        """
        resolved_market = (
            market if market is not None else market_for_symbol(symbol)
        )
        status = "DISABLED"
        try:
            if not self.funded_margin_enabled:
                return None
            if self.paper_account_confirmed:
                status = "PAPER"
                return None
            provider = self.funded_margin_fingerprint_provider
            if provider is None:
                status = "NOT_CONFIGURED"
                return None
            configured = (
                self.funded_margin_account_fingerprint != ""
                and self.funded_margin_requested_quantity > 0
                and self.funded_margin_requested_notional > 0
                and self.funded_margin_requested_risk > 0
            )
            if not configured:
                status = "NOT_CONFIGURED"
                return None
            # CURRENT-credential binding: evaluated LAZILY, fail-closed.
            try:
                current = str(provider() or "")
            except Exception:
                status = "CREDENTIALS_INCOMPLETE"
                return None
            if not current:
                status = "CREDENTIALS_INCOMPLETE"
                return None
            if current != self.funded_margin_account_fingerprint:
                status = "MISMATCH"
                return None
            status = "MATCHED"
            # LANE gates (checked AFTER the binding so diagnostics report
            # the credential state even when the lane is excluded): primary
            # range-lane BUY on the US market only — no passive owner or
            # lane marker, not an opening-momentum entry, not an LLM order,
            # not SELL_SHORT, not HK. Round-2 finding 4: the source markers
            # live BOTH at the context top level AND inside the runner's
            # serialized config_snapshot (where _opening_execution_ledger_
            # context actually writes them); both are checked so a context
            # built by the real runner hand-off can never relax the caps.
            if action != "BUY" or resolved_market != "US":
                return None
            context = self._active_execution_context
            if context.get(_PASSIVE_OWNER_KEY) is not None or context.get(
                EXECUTION_CONTEXT_LANE_KEY,
            ):
                return None
            source_markers = self._funded_margin_source_markers(context)
            if source_markers & _FUNDED_MARGIN_EXCLUDED_SOURCES:
                return None
            # min(raw strategy value, requested value, code bound) per cap;
            # stop_loss_pct is shared and unchanged.
            raw_qty = self._positive_finite_limit(
                self.raw_strategy_max_position_quantity,
            )
            raw_notional = self._positive_finite_limit(
                self.raw_strategy_max_position_notional,
            )
            raw_risk = self._positive_finite_limit(
                self.raw_strategy_max_risk_per_trade,
            )
            if raw_qty is None or raw_notional is None or raw_risk is None:
                status = "NOT_CONFIGURED"
                return None
            return _EntryRiskLimits(
                max_quantity=min(
                    raw_qty,
                    Decimal(self.funded_margin_requested_quantity),
                    Decimal(FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND),
                ),
                max_notional=min(
                    raw_notional,
                    Decimal(str(self.funded_margin_requested_notional)),
                    Decimal(str(FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND)),
                ),
                max_risk=min(
                    raw_risk,
                    Decimal(str(self.funded_margin_requested_risk)),
                    Decimal(str(FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND)),
                ),
                stop_loss_pct=self._positive_finite_limit(
                    self.stop_loss_pct,
                ) or Decimal("0"),
            )
        finally:
            self._funded_margin_last_binding_status = status

    def funded_margin_diagnostics(self) -> dict[str, object]:
        """Observer-only funded-margin exception diagnostics (no secrets)."""
        limits = self._range_entry_limits_for(
            "", "BUY", "US",
        )
        if limits is not None:
            effective: tuple[int | None, float | None, float | None] = (
                int(limits.max_quantity),
                float(limits.max_notional),
                float(limits.max_risk),
            )
        else:
            effective = (None, None, None)
        return {
            "enabled": bool(self.funded_margin_enabled),
            "configured": bool(
                self.funded_margin_enabled
                and not self.paper_account_confirmed
                and self.funded_margin_account_fingerprint != ""
                and self.funded_margin_requested_quantity > 0
                and self.funded_margin_requested_notional > 0
                and self.funded_margin_requested_risk > 0
            ),
            "binding_status": self._funded_margin_last_binding_status,
            "requested_caps": {
                "quantity": self.funded_margin_requested_quantity,
                "notional": self.funded_margin_requested_notional,
                "risk": self.funded_margin_requested_risk,
            },
            "code_bounds": {
                "quantity": FUNDED_MARGIN_MAX_POSITION_QUANTITY_BOUND,
                "notional": FUNDED_MARGIN_MAX_POSITION_NOTIONAL_BOUND,
                "risk": FUNDED_MARGIN_MAX_RISK_PER_TRADE_BOUND,
            },
            "effective_caps": {
                "quantity": effective[0],
                "notional": effective[1],
                "risk": effective[2],
            },
            "factor": (
                None
                if self.margin_safety_factor is None
                else float(self.margin_safety_factor)
            ),
            "last_limiting_factor": self._funded_margin_last_limiting_factor,
        }

    def _funded_margin_source_markers(
        self,
        context: Mapping[str, object],
    ) -> set[str]:
        """Collect execution-source markers from the ACTIVE context.

        Round-2 finding 4: reads the explicit TOP-LEVEL
        ``execution_initiator`` the runner hands off AND the
        ``strategy_source`` markers (top level + inside the serialized
        ``config_snapshot``, where ``_opening_execution_ledger_context``
        actually writes it), so the marker the REAL runner hand-off
        carries is what gates the lane.
        """
        markers: set[str] = set()
        initiator = str(
            context.get(EXECUTION_CONTEXT_INITIATOR_KEY, "") or "",
        ).upper()
        if initiator:
            markers.add(initiator)
        for key in ("strategy_source",):
            top_level = str(context.get(key, "") or "").upper()
            if top_level:
                markers.add(top_level)
            raw_snapshot = context.get("config_snapshot")
            if raw_snapshot:
                try:
                    snapshot = json.loads(str(raw_snapshot))
                except (TypeError, ValueError):
                    snapshot = None
                if isinstance(snapshot, dict):
                    nested = str(
                        snapshot.get(key, "") or "",
                    ).upper()
                    if nested:
                        markers.add(nested)
        return markers

    def _funded_margin_exception_effective_for(
        self,
        symbol: str,
        action: str,
        market: str | None = None,
    ) -> bool:
        """True only when the exception resolves caps for THIS order.

        Thin wrapper over ``_range_entry_limits_for`` for call sites that
        only need the effective/not-effective verdict (e.g. the final
        submit-time session re-check); never used to bypass the resolver.
        """
        return (
            self._range_entry_limits_for(symbol, action, market) is not None
        )

    def _entry_safety_configuration_error(self) -> str | None:
        match self._entry_risk_limits():
            case str() as issue:
                if self._active_passive_owner() is not None:
                    # The passive lane has no stop parameter; only the
                    # quantity/notional caps are safety-relevant for it.
                    passive_limits = self._passive_entry_limits()
                    if isinstance(passive_limits, _EntryRiskLimits):
                        return None
                return issue
            case _EntryRiskLimits():
                return None
            case unreachable:
                assert_never(unreachable)

    def _passive_cash_or_none(self) -> _UsdCashEvidenceLike | None:
        """The strict cash snapshot stored for THIS execute() call, or None."""
        cash = self._active_execution_context.get(_PASSIVE_CASH_KEY)
        if isinstance(cash, _UsdCashEvidenceLike):
            return cash
        return None

    def _passive_hooks_or_none(self) -> _PassiveSubmitHooks | None:
        """The fully-wired hook bundle, or None (passive markers refused)."""
        hooks = self.passive_submit_hooks
        if hooks is not None and passive_protocol.passive_hooks_complete(hooks):
            return hooks
        return None

    def _active_passive_owner(self) -> passive_protocol.PassiveOwner | None:
        """The execution owner stored for THIS execute() call, or None."""
        owner = self._active_execution_context.get(_PASSIVE_OWNER_KEY)
        return owner if isinstance(owner, passive_protocol.PassiveOwner) else None

    def _resolve_passive_entry_policy(
        self,
        request: _PreSubmitRiskRequest,
    ) -> str | None:
        """Resolve the passive-lane policy for this request (READ ONLY).

        Returns ``None`` when the order does not carry the SPY_PASSIVE lane
        execution context (every range order — nothing below changes), or a
        rejection string when it does but no valid executing authorisation
        stands behind it. The lane string must equal ``SPY_PASSIVE``
        EXACTLY. This method performs NO mandate mutation: the submit right
        is consumed later, once, inside ``_final_submission_precheck``.
        """
        context = self._active_execution_context
        lane = str(context.get(EXECUTION_CONTEXT_LANE_KEY, "") or "")
        if not lane:
            return None
        if lane != PASSIVE_LANE:
            return (
                f"execution context carries unknown lane {lane!r}; only the "
                f"{PASSIVE_LANE} lane is recognised, and a lane context must "
                "never fall back to the range path"
            )
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            return (
                "passive lane requested but the passive submit hooks are "
                "unavailable or incomplete; entry denied"
            )
        gate_issue = hooks.current_gate_issue()
        if gate_issue is not None:
            return gate_issue
        owner = self._active_passive_owner()
        if owner is None:
            return (
                "passive lane requested but no execution owner stands behind "
                "this order; entry denied"
            )
        verdict = hooks.resolve_policy(
            owner,
            passive_protocol.PassiveOrderSpec(
                symbol=request.symbol,
                side=_ACTION_TO_SIDE[request.action],
                quantity=request.quantity,
                price=request.price,
            ),
        )
        # R1-3: ONLY an actual ValidatedPassiveIntent is accepted. A None
        # (or any other shape) from the resolver is a denial — never a
        # silent fall-through to the range path.
        if not isinstance(
            verdict, passive_protocol.ValidatedPassiveIntent,
        ):
            if isinstance(verdict, passive_protocol.PassiveRejection):
                return verdict.reason
            return (
                "passive policy resolver returned no valid intent "
                f"({type(verdict).__name__}); passive entry denied"
            )
        self._active_execution_context[_PASSIVE_VALIDATED_KEY] = verdict
        return None

    def _sized_entry_quantity(self) -> Decimal | None:
        """Caller-supplied sizing for the passive lane, or None.

        Stored in the per-execution context (scoped to one ``execute()``
        call under the submission lock), never as service state.
        """
        raw = self._active_execution_context.get(
            EXECUTION_CONTEXT_SIZED_QUANTITY_KEY,
        )
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return Decimal("NaN")

    def _validate_passive_sized_quantity(
        self,
        request: _PreSubmitRiskRequest,
    ) -> str | None:
        """The passive lane's sizing channel, validated at the boundary.

        ``sized_quantity`` (threaded via the execution context) is accepted
        ONLY alongside a valid passive authorisation, and it must agree with
        the submitted quantity and be a positive integer (review 2026-09-29,
        item 2). Without the lane context it is refused before this method
        runs (see the None branch above).
        """
        sized = self._sized_entry_quantity()
        if sized is None:
            return (
                "SPY_PASSIVE mandate entries must carry sized_quantity from "
                "the mandate allotment sizing"
            )
        if not sized.is_finite() or sized <= 0:
            return "sized_quantity must be finite and greater than zero"
        if sized != sized.to_integral_value():
            return "sized_quantity must be an integer share count"
        if sized != request.quantity:
            return (
                "sized_quantity does not match the submitted quantity; the "
                "mandate sizing cannot be substituted"
            )
        return None

    def _passive_commission_for_request(
        self,
        *,
        price: Decimal,
        quantity: Decimal,
    ) -> Decimal:
        """Commission for the passive allotment check at the boundary.

        Always the approved US §9.8 measured model recomputed at the FINAL
        approved price — never a caller-supplied context value (review
        2026-09-29, item 3): the context must not be able to lower the fee.
        """
        return _accounting_order_fee(
            model=ACCOUNTING_FEE_MODEL_US_SEC98,
            market="US",
            price=price,
            quantity=quantity,
            legacy_rate=Decimal("0"),
        )

    def _passive_entry_limits(self) -> _EntryRiskLimits | str:
        """Caps for the passive lane: share count and notional only.

        The passive branch returns before the stop-distance risk check, so
        ``max_risk`` / ``stop_loss_pct`` are never read there; they carry
        fail-closed placeholders so an accidental later read rejects rather
        than widens (a zero stop distance is "unavailable", a zero risk cap
        exceeds on any positive risk).
        """
        with self._state_lock:
            raw_max_quantity = self.max_position_quantity
            raw_max_notional = self.max_position_notional

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
        return _EntryRiskLimits(
            max_quantity=max_quantity,
            max_notional=max_notional,
            max_risk=Decimal("0"),
            stop_loss_pct=Decimal("0"),
        )

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
                if self._active_passive_owner() is not None:
                    # The passive lane has no stop parameter; its caps are
                    # the quantity/notional pair only, so a zero/missing
                    # stop config must not block the passive branch.
                    passive_limits = self._passive_entry_limits()
                    if isinstance(passive_limits, _EntryRiskLimits):
                        limits = passive_limits
                    else:
                        return self._pre_submit_risk_rejection(request, issue)
                else:
                    return self._pre_submit_risk_rejection(request, issue)
            case _EntryRiskLimits() as resolved_limits:
                limits = resolved_limits
            case unreachable:
                assert_never(unreachable)
        # SPY_PASSIVE lane branch: an order bound to an active mandate
        # resolves a passive policy instead of the range stop-distance
        # model. Range orders carry no lane context, resolve None here, and
        # keep the exact $250 / 1% arithmetic below — their path is
        # unchanged. Only the sizing caps differ: notional + commission vs
        # the mandate allotment, with no stop parameter required (a ZERO
        # stop is legal ONLY on this branch; a range stop=0 still rejects).
        passive_validated: passive_protocol.ValidatedPassiveIntent | None = None
        lane_in_context = bool(
            self._active_execution_context.get(EXECUTION_CONTEXT_LANE_KEY),
        )
        passive_issue = self._resolve_passive_entry_policy(request)
        if passive_issue is not None:
            return self._pre_submit_risk_rejection(request, passive_issue)
        validated = self._active_execution_context.get(_PASSIVE_VALIDATED_KEY)
        if lane_in_context:
            # R1-3: a lane marker REQUIRES a validated intent — a resolver
            # that produced anything else must never fall back to the range
            # branch (the None-verdict case is rejected above; this guards
            # a validated key that vanished between the two reads).
            if not isinstance(
                validated, passive_protocol.ValidatedPassiveIntent,
            ):
                return self._pre_submit_risk_rejection(
                    request,
                    "passive lane requested but no validated intent stands "
                    "behind this order; entry denied",
                )
            sized_issue = self._validate_passive_sized_quantity(request)
            if sized_issue is not None:
                return self._pre_submit_risk_rejection(request, sized_issue)
            passive_limits_result = self._passive_entry_limits()
            match passive_limits_result:
                case str() as issue:
                    return self._pre_submit_risk_rejection(request, issue)
                case _EntryRiskLimits() as passive_limits:
                    limits = passive_limits
                    passive_validated = validated
                case unreachable:
                    assert_never(unreachable)
        elif not lane_in_context and self._sized_entry_quantity() is not None:
            # ``sized_quantity`` is the passive lane's sizing channel; it is
            # accepted ONLY when the request also resolves a valid passive
            # authorisation. A plain range caller passing it must be
            # rejected, never silently re-sized by margin power.
            return self._pre_submit_risk_rejection(
                request,
                "sized_quantity is only accepted for an order bound "
                "to a valid SPY_PASSIVE mandate authorisation; "
                "range entries must size from buying power",
            )

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
        # Funded-margin exception (contract §E): re-resolve the binding at
        # the boundary. Effective => the exception caps replace the clamped
        # trio for THIS order AND broker margin capacity is re-estimated at
        # the FINAL approved price; a projected quantity above
        # floor(factor x capacity) or any resolved cap rejects. No longer
        # effective (e.g. credential rotation between sizing and submit)
        # => the clamped funded caps apply and an oversized order fails
        # closed. Flag-off / paper / unbound: NOT one extra broker call.
        funded_margin_limits = self._range_entry_limits_for(
            request.symbol, request.action,
        )
        if funded_margin_limits is not None:
            limits = funded_margin_limits
            capacity = self._positive_finite_limit(
                broker.estimate_margin_max_quantity(
                    request.symbol,
                    "BUY",
                    approved_price,
                    "USD",
                ),
            )
            if capacity is None:
                return self._pre_submit_risk_rejection(
                    request,
                    "funded margin capacity is unavailable at the final "
                    "approved price",
                )
            raw_pre_submit_factor = self.margin_safety_factor
            pre_submit_factor = self._positive_finite_limit(
                ENTRY_BUYING_POWER_USAGE
                if raw_pre_submit_factor is None
                else raw_pre_submit_factor,
            )
            if pre_submit_factor is None or pre_submit_factor > 1:
                return self._pre_submit_risk_rejection(
                    request,
                    "funded margin safety factor must be greater than "
                    "zero and at most one",
                )
            capacity_qty = int(capacity * pre_submit_factor)
            if request.quantity > Decimal(capacity_qty):
                return self._pre_submit_risk_rejection(
                    request,
                    f"projected quantity {request.quantity} exceeds margin "
                    f"capacity {capacity_qty} at the final approved price",
                )
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
        if passive_validated is not None:
            # SPY_PASSIVE lane: risk is the full notional + commission and is
            # checked against the mandate allotment — no stop parameter. The
            # range path keeps the $250 / 1% arithmetic below, unchanged.
            passive_issue = validate_passive_entry_risk(
                resolved=ResolvedPassivePolicy(
                    allotment_usd=passive_validated.allotment_usd,
                ),
                quantity=request.quantity,
                approved_price=approved_price,
                max_quantity=limits.max_quantity,
                max_notional=limits.max_notional,
                commission=self._passive_commission_for_request(
                    price=approved_price,
                    quantity=request.quantity,
                ),
            )
            if passive_issue is not None:
                return self._pre_submit_risk_rejection(request, passive_issue)
            # Strict cash evidence at the FINAL approved price (contract
            # §Cash API): validated at the boundary and revalidated after
            # the submit CAS by _claim_passive_submission_right.
            final_fee = self._passive_commission_for_request(
                price=approved_price,
                quantity=request.quantity,
            )
            cash_issue = passive_protocol.validate_cash_evidence(
                cash=self._passive_cash_or_none(),
                quantity=request.quantity,
                approved_price=approved_price,
                fee=final_fee,
                now=self._passive_now(),
            )
            if cash_issue is not None:
                return self._pre_submit_risk_rejection(
                    request, f"strict cash: {cash_issue}",
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
        action = "SELL_SHORT" if side == "SELL" else "BUY"
        # Funded-margin exception (contract §C/§D): resolve the range-lane
        # caps FIRST so an effective exception replaces the clamped trio
        # for THIS call only; every other caller keeps _entry_risk_limits.
        # Flag-off / paper / unbound => None => byte-for-byte legacy path.
        funded_margin_limits = self._range_entry_limits_for(
            symbol, action,
        )
        limits_result = (
            funded_margin_limits
            if funded_margin_limits is not None
            else self._entry_risk_limits()
        )
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
            action,
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
        if funded_margin_limits is not None and factor > 1:
            # Contract §D: full-margin sizing requires 0 < factor <= 1;
            # anything above one is an invalid configuration, never an
            # over-leveraged order.
            logger.error(
                "%s: funded-margin safety factor %s exceeds 1.0; quantity "
                "is zero until the factor is fixed",
                side,
                factor,
            )
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

        # Funded-margin exception (contract §D): when a cap binds below
        # floor(factor x margin capacity) the capped quantity still
        # PROCEEDS (an authorized smaller trade beats no trade) but is
        # never silent — the limiting factor is recorded for the trade
        # event payload and diagnostics.
        if funded_margin_limits is not None:
            full_margin_qty = int(max_qty * factor)
            limiting_factor: str | None = None
            if candidate < Decimal(full_margin_qty):
                if candidate >= max(Decimal("0"), remaining_qty):
                    limiting_factor = "QUANTITY_CAP"
                elif candidate >= risk_qty:
                    limiting_factor = "RISK_CAP"
                elif candidate >= notional_qty:
                    limiting_factor = "NOTIONAL_CAP"
                else:
                    limiting_factor = "MARGIN_CAPACITY"
            self._funded_margin_last_limiting_factor = limiting_factor
            if limiting_factor is not None:
                self._active_execution_context.setdefault(
                    "funded_margin_limiting_factor",
                    limiting_factor,
                )
                self._active_execution_context.setdefault(
                    "funded_margin_full_margin_qty",
                    full_margin_qty,
                )
                logger.info(
                    "full margin capped by %s: margin capacity %d shares, "
                    "capped to %d shares for %s",
                    limiting_factor,
                    full_margin_qty,
                    int(candidate),
                    symbol,
                )

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
    def _normalize_price_floor(
        symbol: str,
        action: str,
        floor: Decimal,
    ) -> Decimal:
        """Round the reduction price bound toward safety on the market tick."""
        rounding = ROUND_CEILING if action == "SELL" else ROUND_FLOOR
        upper_symbol = symbol.upper()
        if upper_symbol.endswith(".US"):
            return floor.quantize(US_PRICE_TICK, rounding=rounding)
        if upper_symbol.endswith(".HK"):
            tick = _hk_tick_for(floor)
            steps = (floor / tick).to_integral_value(rounding=rounding)
            return (steps * tick).quantize(tick)
        return floor

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

    def _overnight_entry_spread_refusal(
        self,
        symbol: str,
        bid_price: Decimal,
        ask_price: Decimal,
    ) -> OrderStatus | None:
        """Refuse a long OVERNIGHT entry when BBO is missing or wider than 0.10%.

        Exits never reach this helper. PRE/POST/RTH are unaffected.
        """
        context = self._extended_hours_context
        if context is None or len(context) < 2 or context[1] != "OVERNIGHT":
            return None
        mid = (ask_price + bid_price) / Decimal("2")
        if (
            not bid_price.is_finite()
            or not ask_price.is_finite()
            or bid_price <= 0
            or ask_price <= 0
            or not mid.is_finite()
            or mid <= 0
            or (ask_price - bid_price) / mid * Decimal("100")
            > _OVERNIGHT_ENTRY_MAX_SPREAD_PCT
        ):
            return self._skip_order(
                symbol,
                "BUY",
                "overnight spread too wide",
                skip_category="FEE",
            )
        return None

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
        try:
            bid_price = Decimal(str(bid))
            ask_price = Decimal(str(ask))
        except Exception:
            bid_price = Decimal("0")
            ask_price = Decimal("0")
        overnight_spread = self._overnight_entry_spread_refusal(
            symbol, bid_price, ask_price,
        )
        if overnight_spread is not None:
            return overnight_spread
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
        return OrderStatus(
            "", _SKIPPED_ORDER_STATUS, reason=reason, skip_category=skip_category,
        )

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

    def _record_positive_sizing(self, is_funnel_primary: bool) -> None:
        """Observe sizing without acquiring runner locks inside execution."""
        if self.decision_funnel is not None and is_funnel_primary:
            self.decision_funnel.record_sized_quantity_positive()

    def _normalize_board_lot_quantity(
        self, symbol: str, action: str, raw_quantity: Decimal,
    ) -> _BoardLotNormalization:
        """Require fresh lots for exposure, but preserve proven reductions."""
        is_entry = action in _ENTRY_ACTIONS
        day = trade_day_for("HK")
        try:
            resolution = (
                BoardLotResolution.for_unresolved(symbol)
                if self._board_lot_resolver is None
                else self._board_lot_resolver(symbol)
            )
        except Exception:
            if _BOARD_LOT_LOG_THROTTLE.should_log(f"resolve:{symbol}"):
                logger.warning(
                    "board lot resolution failed for %s; treating as unknown (suppressed=%d)",
                    symbol, _BOARD_LOT_LOG_THROTTLE.take_suppressed_count(),
                    exc_info=True,
                )
            resolution = BoardLotResolution(symbol, None, "UNKNOWN")

        quantity = raw_quantity
        lot_size = resolution.lot_size
        degraded = False
        issue: str | None = None
        skip_category = "POSITION"
        match resolution.source:
            case "FRESH":
                assert lot_size is not None
                quantity = quantize_to_board_lot(raw_quantity, lot_size)
                if quantity == 0:
                    if is_entry:
                        # A cap under one lot makes the symbol un-enterable at
                        # any buying power, so it must not read as a shortfall.
                        cap = self.max_position_quantity
                        capped_by_config = cap is not None and cap < lot_size
                        issue = (
                            f"position quantity cap {cap} is below one board lot "
                            f"of {lot_size} for {symbol}; entries are impossible "
                            f"until the cap covers a full lot"
                            if capped_by_config else
                            f"entry quantity {raw_quantity} is below one board lot of {lot_size} for {symbol}"
                        )
                    else:
                        issue = (
                            f"exit quantity {raw_quantity} is below one board lot of {lot_size} for {symbol}; odd-lot liquidation required"
                        )
            case "STALE":
                lot_size = resolution.stale_lot_size
                if is_entry:
                    quantity = Decimal("0")
                    issue = f"board lot for {symbol} is not validated for session {day}; entry denied"
                    skip_category = "RISK"
                else:
                    degraded = True
                    if lot_size is not None:
                        quantity = quantize_to_board_lot(raw_quantity, lot_size) or raw_quantity
            case "UNKNOWN":
                if is_entry:
                    quantity = Decimal("0")
                    issue = f"board lot size for {symbol} is unknown; entry denied"
                    skip_category = "RISK"
                else:
                    degraded = True
            case unreachable:
                assert_never(unreachable)

        if degraded and self._degraded_lot_rejections.get(symbol) == (quantity, day):
            issue = f"degraded exit quantity {quantity} for {symbol} was already rejected this session; awaiting board-lot refresh"
            quantity = Decimal("0")
            skip_category = "RISK"
        return _BoardLotNormalization(
            quantity=quantity, lot_size=lot_size, lot_source=resolution.source,
            residual=Decimal("0") if is_entry else raw_quantity - quantity,
            degraded=degraded, issue=issue, skip_category=skip_category,
        )

    def _report_board_lot_residual(
        self, symbol: str, norm: _BoardLotNormalization, position_quantity: Decimal,
    ) -> None:
        if norm.residual <= 0 or norm.lot_size is None or self._record_board_lot_residual is None:
            return
        try:
            self._record_board_lot_residual(symbol, norm.residual, norm.lot_size, position_quantity)
        except Exception:
            if _BOARD_LOT_LOG_THROTTLE.should_log(f"residual:{symbol}"):
                logger.warning(
                    "board lot residual recording failed for %s (suppressed=%d)",
                    symbol, _BOARD_LOT_LOG_THROTTLE.take_suppressed_count(),
                    exc_info=True,
                )

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
        is_funnel_primary: bool = False,
        sized_quantity: Decimal | None = None,
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "BUY", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("BUY: price <= 0, price=%s", price)
            return None
        if sized_quantity is not None:
            # Caller-sized entry — the SPY_PASSIVE lane's sizing channel.
            # The boundary has ALREADY proved a valid passive authorisation
            # for this order and validated the strict USD cash evidence at
            # the final approved price (see pre_submit_risk_check); here we
            # only double-check the evidence is present and still covers
            # the order at this (identical) price: cash, not margin, funds
            # the lane. Range orders never reach this branch.
            if self._active_passive_owner() is None:
                return self._skip_order(
                    symbol,
                    "BUY",
                    "sized order carries no passive execution owner; "
                    "entry denied",
                    skip_category="RISK",
                )
            commission = self._passive_commission_for_request(
                price=price,
                quantity=sized_quantity,
            )
            cash_issue = passive_protocol.validate_cash_evidence(
                cash=self._passive_cash_or_none(),
                quantity=sized_quantity,
                approved_price=price,
                fee=commission,
                now=self._passive_now(),
            )
            if cash_issue is not None:
                return self._skip_order(
                    symbol,
                    "BUY",
                    f"strict cash: {cash_issue}",
                    skip_category="RISK",
                )
            qty = sized_quantity
        else:
            qty = Decimal(self._entry_quantity_from_margin_power(broker, symbol, "BUY", price, cash_currency))
        if qty <= 0:
            return self._skip_order(
                symbol,
                "BUY",
                "entry quantity is zero after buying-power and position checks",
                skip_category="POSITION",
            )
        self._record_positive_sizing(is_funnel_primary)
        norm = self._normalize_board_lot_quantity(symbol, "BUY", qty)
        if norm.issue is not None:
            return self._skip_order(symbol, "BUY", norm.issue, skip_category=norm.skip_category)
        qty = norm.quantity
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
            fee_model=(
                ACCOUNTING_FEE_MODEL_US_SEC98
                if self._active_passive_owner() is not None
                else str(
                    self._active_execution_context.get(
                        "accounting_fee_model", "",
                    ) or "",
                )
            ),
        )
        passive_owner_fill = self._active_passive_owner()
        if passive_owner_fill is not None:
            pending = dataclass_replace(
                pending,
                passive_owner_ref=_passive_owner_ref_string(
                    passive_owner_fill,
                ),
            )
        self._finalize_pending_fill_once(
            pending, order_status, risk=risk, notifier=notifier,
            notify_risk_event=notify_risk_event,
        )
        logger.info("BUY: %s qty=%s price=%s", symbol, fill_qty, fill_price)
        if self._active_execution_context.get(_PASSIVE_ESCALATED_KEY):
            # Phase2a W2: the receipt escalated (e.g. an ACTUAL overfill
            # above the immutable intent) and the fills are NOW accounted
            # above; surface the explicit UNCERTAIN status (the escalation
            # collaborators — pause/incident/sink — already ran).
            return OrderStatus(
                str(pending.broker_order_id or ""),
                "UNCERTAIN",
                reason=(
                    "ORDER_RECONCILIATION_UNCERTAIN: "
                    f"{PASSIVE_LANE} receipt escalated to uncertain; "
                    "actual broker fills were accounted"
                ),
            )
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
        extended_take_profit: bool = False,
        is_funnel_primary: bool = False,
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
        if extended_take_profit:
            tracked = self.tracked_position(symbol)
            if (
                tracked is not None
                and tracked.side == "LONG"
                and qty > tracked.quantity
            ):
                qty = tracked.quantity

        self._record_positive_sizing(is_funnel_primary)
        norm = self._normalize_board_lot_quantity(symbol, "SELL", qty)
        self._report_board_lot_residual(symbol, norm, Decimal(str(long_pos.quantity)))
        if norm.issue is not None:
            return self._skip_order(symbol, "SELL", norm.issue, skip_category=norm.skip_category)
        qty = norm.quantity
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
        if norm.degraded and order_status is not None and order_status.status == "REJECTED":
            self._degraded_lot_rejections[symbol] = (qty, trade_day_for("HK"))
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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
            ),
        )
        self._finalize_pending_fill_once(
            pending, order_status, risk=risk, notifier=notifier,
            notify_risk_event=notify_risk_event,
        )
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
        is_funnel_primary: bool = False,
    ) -> OrderStatus | None:
        price = self._normalize_limit_price(symbol, "SELL_SHORT", Decimal(str(quote.last_price)))
        if price <= 0:
            logger.warning("SELL_SHORT: price <= 0, price=%s", price)
            return None

        qty = Decimal(self._entry_quantity_from_margin_power(broker, symbol, "SELL", price, cash_currency))
        if qty <= 0:
            return self._skip_order(
                symbol,
                "SELL_SHORT",
                "entry quantity is zero after buying-power and position checks",
                skip_category="POSITION",
            )

        self._record_positive_sizing(is_funnel_primary)
        norm = self._normalize_board_lot_quantity(symbol, "SELL_SHORT", qty)
        if norm.issue is not None:
            return self._skip_order(symbol, "SELL_SHORT", norm.issue, skip_category=norm.skip_category)
        qty = norm.quantity
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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
            ),
        )
        self._finalize_pending_fill_once(
            pending, order_status, risk=risk, notifier=notifier,
            notify_risk_event=notify_risk_event,
        )
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
        is_funnel_primary: bool = False,
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

        self._record_positive_sizing(is_funnel_primary)
        norm = self._normalize_board_lot_quantity(symbol, "BUY_TO_COVER", qty)
        self._report_board_lot_residual(symbol, norm, Decimal(str(pos.quantity)))
        if norm.issue is not None:
            return self._skip_order(symbol, "BUY_TO_COVER", norm.issue, skip_category=norm.skip_category)
        qty = norm.quantity
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
        if norm.degraded and order_status is not None and order_status.status == "REJECTED":
            self._degraded_lot_rejections[symbol] = (qty, trade_day_for("HK"))
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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
            ),
        )
        self._finalize_pending_fill_once(
            pending, order_status, risk=risk, notifier=notifier,
            notify_risk_event=notify_risk_event,
        )
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
            # Round-3 finding 2: reset the frozen verdict before each
            # submit; _process_submitted_order re-resolves it while the
            # execution context for THIS order is live.
            self._funded_margin_applied_at_submit = False
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
            # Round-2 finding 2 (P1): final-submit session re-check. When
            # the funded-margin exception is EFFECTIVE for this order
            # (range US BUY, binding MATCHED — resolved fresh here), the
            # session calendar is re-consulted AFTER every blocking step
            # (pre-submit boundary, capacity re-estimate, policy gates)
            # and immediately BEFORE the single broker mutation: a clock
            # that crossed the RTH close or the entry cutoff
            # during those queries skips with SESSION and never submits.
            # Flag-off / paper / unbound / reductions resolve ineffective
            # => no new calendar call, identical behaviour and call shapes.
            if approved_order.action in _ENTRY_ACTIONS and (
                self._funded_margin_exception_effective_for(
                    approved_order.symbol,
                    approved_order.action,
                )
            ):
                session_market = market_for_symbol(approved_order.symbol)
                if not self._final_submit_session_open(
                    session_market,
                    symbol=approved_order.symbol,
                    trading_session_mode=self._execution_session_mode,
                ):
                    # Extended-not-effective keeps today's RTH wording so
                    # flag-off / paper / unbound paths stay identical.
                    if self._extended_hours_trading_effective():
                        session_reason = (
                            f"execution session closed before final "
                            f"submission for {session_market}"
                        )
                    else:
                        session_reason = (
                            f"RTH session ended before final submission "
                            f"for {session_market}"
                        )
                    return self._skip_order(
                        approved_order.symbol,
                        approved_order.action,
                        session_reason,
                        skip_category="SESSION",
                    )
                if self._entry_cutoff_active(session_market):
                    return self._skip_order(
                        approved_order.symbol,
                        approved_order.action,
                        (
                            f"entry cutoff within "
                            f"{self.entry_cutoff_minutes_before_close} "
                            "minutes of close crossed before final "
                            "submission"
                        ),
                        skip_category="SESSION",
                    )
            submit_started_at = datetime.now(timezone.utc)
            submit_started_monotonic = time.perf_counter()
            # The passive submit right (if any) was consumed by the precheck
            # above; this flag now guards outcome recording only.
            passive_owner_at_submit = self._active_passive_owner()

            def submit_approved_order() -> OrderResult | OrderStatus:
                try:
                    if approved_order.outside_rth is not None:
                        return broker.submit_limit_order(
                            approved_order.symbol,
                            approved_order.side,
                            approved_order.quantity,
                            approved_order.price,
                            outside_rth=approved_order.outside_rth,
                        )
                    return broker.submit_limit_order(
                        approved_order.symbol,
                        approved_order.side,
                        approved_order.quantity,
                        approved_order.price,
                    )
                except ExtendedHoursUnsupportedError as exc:
                    TradeExecutionService._extended_hours_sdk_unsupported = True
                    key = self._active_extended_hours_key()
                    if key is not None:
                        self._extended_hours_terminal_outcome(
                            key,
                            unsupported=True,
                            notify_risk_event=notify_risk_event,
                        )
                    if restore_engine_snapshot is not None and engine_snapshot is not None:
                        restore_engine_snapshot(engine_snapshot)
                    return self._skip_order(symbol, action, str(exc), skip_category="RISK")
                except Exception as exc:
                    raise BrokerSubmissionUncertainError(
                        action=approved_order.action,
                        symbol=approved_order.symbol,
                        cause=str(exc),
                    ) from exc

            if approved_order.protective_commit_required:
                approved_phase = self._approval_phase(approved_order.symbol)
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
                    phase_refusal = self._phase_mismatch(
                        approved_order.symbol,
                        approved_order.action,
                        market_for_symbol(approved_order.symbol),
                        approved_phase,
                    )
                    if phase_refusal is not None:
                        risk.revoke_protective_exits()
                        return phase_refusal
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

            if isinstance(broker_result, OrderStatus):
                return broker_result
            # Immediately preserve the broker response/id before any
            # orders/pending/settlement processing (contract §9): for the
            # passive lane this binds ORDER_KNOWN at once; a recording
            # failure is UNCERTAIN, never success.
            if passive_owner_at_submit is not None:
                recorded = self._record_passive_receipt(
                    passive_owner_at_submit, broker_result,
                    risk=risk, notifier=notifier,
                )
                if recorded is not None:
                    return recorded
            if passive_owner_at_submit is not None:
                # R1-4: ANY exception after the broker call (orders/
                # pending/settlement processing) is UNCERTAIN for the lane —
                # never a success-like ORDER_KNOWN and never a swallow.
                # Phase2a W2: an ESCALATED receipt (e.g. an actual
                # overfill) keeps flowing through this normal processing so
                # the real fills are accounted; the dedicated entry's
                # finalizer surfaces the explicit UNCERTAIN afterwards.
                try:
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
                except _PassiveSubmitUncertain as exc:
                    return OrderStatus(
                        str(getattr(broker_result, "broker_order_id", "") or ""),
                        "UNCERTAIN",
                        reason=str(exc),
                    )
                except BrokerSubmissionUncertainError as exc:
                    return self._escalate_passive_uncertain(
                        passive_owner_at_submit,
                        str(getattr(broker_result, "broker_order_id", "") or ""),
                        str(exc),
                        risk=risk,
                        notifier=notifier,
                    )
                except Exception as exc:
                    return self._escalate_passive_uncertain(
                        passive_owner_at_submit,
                        str(getattr(broker_result, "broker_order_id", "") or ""),
                        (
                            f"post-acceptance processing raised "
                            f"{type(exc).__name__}: {exc}"
                        ),
                        risk=risk,
                        notifier=notifier,
                    )
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

    def _record_passive_fill_observation(
        self,
        pending: _PendingOrder,
        order_status: OrderStatus,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        """Phase2a W2: observe an ACCOUNTED fill on the passive mandate.

        Runs AFTER ``_book_fill`` — the actual broker quantity has already
        been booked through the existing tracked/settlement path; this only
        reports the cumulative broker observation to the mandate. Marked
        refs only (delayed fills carry ``passive_owner_ref``; an immediate
        fill may still hold the active-context owner). An ESCALATED or
        failing write escalates to UNCERTAIN (pause + incident + sink)
        WITHOUT ever trimming or undoing the booked fill. No double
        booking: ``_book_fill`` remains the single accounting authority;
        this is observation only.
        """
        ref_string = str(pending.passive_owner_ref or "")
        owner: passive_protocol.PassiveOwner | None = None
        if ref_string:
            try:
                owner = self._passive_owner_from_ref(ref_string)
            except Exception:
                owner = None
        if owner is None:
            owner = self._active_passive_owner()
        if owner is None:
            return
        broker_id = str(pending.broker_order_id or "")
        status_text = str(getattr(order_status, "status", "") or "")
        if not broker_id or not status_text:
            return
        cumulative_qty = self._resolved_decimal(
            order_status, "executed_quantity", Decimal("0"),
        )
        cumulative_price = self._resolved_decimal(
            order_status, "executed_price", Decimal("0"),
        )
        fact = passive_protocol.PassiveOutcomeFact(
            outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            broker_order_id=broker_id,
            broker_status=status_text,
            executed_quantity=(
                cumulative_qty if cumulative_qty > 0 else None
            ),
            executed_price=(
                cumulative_price if cumulative_price > 0 else None
            ),
        )
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            return
        try:
            write_result = hooks.record_outcome(owner, fact)
        except Exception as exc:
            self._escalate_passive_uncertain(
                owner,
                broker_id,
                (
                    f"recording the accounted fill observation failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                risk=risk,
                notifier=notifier,
            )
            return
        if self._passive_write_escalated(write_result):
            self._escalate_passive_uncertain(
                owner,
                broker_id,
                (
                    f"the accounted fill observation escalated "
                    f"(status {status_text!r}, cumulative "
                    f"{cumulative_qty}@{cumulative_price})"
                ),
                risk=risk,
                notifier=notifier,
            )

    def _record_passive_receipt_progress(
        self,
        pending: _PendingOrder,
        order_status: OrderStatus,
        *,
        risk: RiskController | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> None:
        """Record same-id receipt PROGRESS on the passive mandate (R1-5c).

        Works purely from the durable pending owner ref — never the (long
        cleared) active execution context. Final-remediation finding 4: a
        NONEMPTY passive ref whose owner cannot be restored, or whose
        outcome write fails, is a CRITICAL persistence failure — it must
        pause and record an unresolved-reference incident, never sink to a
        debug log while the mandate stays success-like.
        """
        ref_string = str(pending.passive_owner_ref or "")
        if not ref_string:
            return
        broker_id = str(pending.broker_order_id or "")
        status_text = str(order_status.status or "")
        try:
            owner = self._passive_owner_from_ref(ref_string)
        except Exception as exc:
            # The intent lookup itself failed (DB error propagated by the
            # hook, not catch-to-None): a critical persistence failure.
            self._escalate_unresolved_passive_reference(
                ref_string,
                (
                    f"the passive owner lookup raised while recording "
                    f"receipt {status_text!r}: {type(exc).__name__}: {exc}"
                ),
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        if owner is None:
            self._escalate_unresolved_passive_reference(
                ref_string,
                (
                    "the pending passive owner could not be restored from "
                    f"its durable reference (receipt {status_text!r})"
                ),
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            self._escalate_unresolved_passive_reference(
                ref_string,
                "passive submit hooks are unavailable for receipt progress",
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        if not broker_id or not status_text:
            return
        try:
            write_result = hooks.record_outcome(
                owner,
                passive_protocol.PassiveOutcomeFact(
                    outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
                    broker_order_id=broker_id,
                    broker_status=status_text,
                    executed_quantity=order_status.executed_quantity,
                    executed_price=order_status.executed_price,
                ),
            )
        except Exception as exc:
            # A genuine receipt with new facts was observed but could not
            # be persisted: escalate (pause + incident), keep the receipt
            # facts in the incident, never a debug-only swallow.
            self._escalate_unresolved_passive_reference(
                ref_string,
                (
                    f"recording receipt progress for {broker_id} failed: "
                    f"{type(exc).__name__}: {exc}"
                ),
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        if self._passive_write_escalated(write_result):
            # Phase2a W2: the typed result escalated (conflicting/unknown
            # status or an overfill above the immutable intent): surface
            # it through the full uncertainty path (pause + incident +
            # sink) — never a silent sticky-uncertain with 0 incidents.
            self._escalate_passive_uncertain(
                owner,
                broker_id,
                (
                    f"receipt progress escalated (status {status_text!r})"
                ),
                risk=risk,
                notifier=None,
            )

    def _escalate_unresolved_passive_reference(
        self,
        reference: str,
        issue: str,
        *,
        broker_order_id: str | None = None,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        """Escalate an ownerless/failed passive persistence situation.

        Final-remediation finding 4: pause with the REAL risk controller
        and notify regardless of whether the owner could be resolved or the
        incident could be persisted. ``record_unresolved_reference`` (the
        Y-side hook) records the incident without inventing mandate
        authority; when it is absent or itself fails, the pause + CRITICAL
        log still happen — durable success is never faked.
        """
        reason = (
            f"{ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL} "
            f"{PASSIVE_LANE} broker order "
            f"{broker_order_id or '<unknown>'} is UNCERTAIN: {issue} "
            f"(passive reference {reference[:8]}… retained)"
        )
        hooks = self._passive_hooks_or_none()
        recorder = getattr(hooks, "record_unresolved_reference", None)
        if callable(recorder):
            try:
                recorder(
                    reference,
                    issue,
                    broker_order_id=broker_order_id,
                )
            except Exception:
                logger.exception(
                    "failed to record the unresolved passive reference "
                    "incident"
                )
        if risk is not None:
            try:
                if not risk.paused:
                    risk.pause(reason, auto_resumable=False)
            except Exception:
                logger.exception(
                    "failed to pause for an unresolved passive reference"
                )
        if notify_risk_event is not None:
            try:
                notify_risk_event("PASSIVE_MANDATE_SUBMIT_UNCERTAIN", reason)
            except Exception:
                logger.exception(
                    "failed to notify an unresolved passive reference"
                )
        logger.critical(reason)

    def _escalate_passive_pending_uncertain(
        self,
        pending: _PendingOrder,
        issue: str,
        *,
        risk: RiskController | None,
        notify_risk_event: _NotifyRiskEvent | None,
    ) -> None:
        """Escalate a post-acceptance failure to mandate UNCERTAIN (R1-5).

        Final-remediation finding 4: the pause happens with the REAL risk
        controller REGARDLESS of whether the owner could be restored or the
        mandate write succeeded — an unresolvable owner escalates through
        the unresolved-reference incident path instead of silently doing
        nothing.
        """
        ref_string = str(pending.passive_owner_ref or "")
        broker_id = str(pending.broker_order_id or "")
        if not ref_string:
            # B3: an ORDINARY RANGE order (empty/None passive ref) must
            # keep its original range failure behaviour — this passive
            # escalation is a complete NO-OP for it: no pause overwrite,
            # no passive callbacks, no incidents. The range path's own
            # failure handling (which already ran or will run) owns it.
            return
        owner: passive_protocol.PassiveOwner | None = None
        lookup_failed = False
        try:
            owner = self._passive_owner_from_ref(ref_string)
        except Exception as exc:
            lookup_failed = True
            self._escalate_unresolved_passive_reference(
                ref_string,
                (
                    f"the pending passive owner lookup raised while "
                    f"escalating '{issue}': {type(exc).__name__}: {exc}"
                ),
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
        if owner is None and not lookup_failed:
            self._escalate_unresolved_passive_reference(
                ref_string,
                (
                    f"the pending passive owner could not be restored while "
                    f"escalating '{issue}'"
                ),
                broker_order_id=broker_id,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        assert owner is not None
        reason = (
            f"{ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL} "
            f"{PASSIVE_LANE} broker order {broker_id} is UNCERTAIN: {issue}"
        )
        hooks = self._passive_hooks_or_none()
        mandate_write_failed = False
        if hooks is not None:
            try:
                hooks.record_outcome(
                    owner,
                    passive_protocol.PassiveOutcomeFact(
                        outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                        broker_order_id=broker_id,
                        reason=issue,
                    ),
                )
            except Exception as exc:
                mandate_write_failed = True
                logger.exception(
                    "failed to persist the passive pending UNCERTAIN outcome",
                )
                self._record_unresolved_reference_best_effort(
                    ref_string,
                    (
                        f"persisting the UNCERTAIN outcome for {broker_id} "
                        f"failed: {type(exc).__name__}: {exc}"
                    ),
                    broker_order_id=broker_id,
                )
        # B3/P2: a MARKED passive settlement/receipt failure always leaves
        # an incident record with the real durable owner reference and the
        # known broker id — never only a mandate write.
        self._record_unresolved_reference_best_effort(
            _passive_owner_ref_string(owner),
            issue,
            broker_order_id=broker_id or None,
        )
        if risk is not None:
            try:
                if not risk.paused:
                    risk.pause(reason, auto_resumable=False)
            except Exception:
                logger.exception("failed to pause for uncertain passive pending")
        if notify_risk_event is not None:
            try:
                notify_risk_event("PASSIVE_MANDATE_SUBMIT_UNCERTAIN", reason)
            except Exception:
                logger.exception("failed to notify uncertain passive pending")
        logger.critical(reason)

    def _record_unresolved_reference_best_effort(
        self,
        reference: str,
        reason: str,
        *,
        broker_order_id: str | None = None,
    ) -> None:
        """Best-effort incident record without touching the mandate row."""
        hooks = self._passive_hooks_or_none()
        recorder = getattr(hooks, "record_unresolved_reference", None)
        if not callable(recorder):
            return
        try:
            recorder(reference, reason, broker_order_id=broker_order_id)
        except Exception:
            logger.exception(
                "failed to record the unresolved passive reference incident"
            )

    def _passive_owner_from_ref(
        self,
        ref_string: str,
    ) -> passive_protocol.PassiveOwner | None:
        """Rebuild the owner from the durable 'mandate:claim:exec' ref.

        DB errors from the hook reader PROPAGATE (final-remediation: the
        Y-side ``owner_intent_for`` must surface failures rather than
        catch-to-None; callers treat an exception as an escalation, never
        as "no passive order"). ``None`` for a nonempty ref still means the
        row/facts are gone — also escalated by the callers.
        """
        if not ref_string:
            return None
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            return None
        parts = ref_string.split(":")
        if len(parts) != 3:
            return None
        try:
            mandate_id = int(parts[0])
        except ValueError:
            return None
        claim_token, execution_token = parts[1], parts[2]
        if not claim_token or not execution_token:
            return None
        reader = getattr(hooks, "owner_intent_for", None)
        if callable(reader):
            intent = reader(mandate_id, claim_token)
            if isinstance(
                intent, passive_protocol.ImmutablePassiveIntent,
            ):
                return passive_protocol.PassiveOwner(
                    ref=passive_protocol.PassiveAttemptRef(
                        mandate_id=mandate_id,
                        claim_token=claim_token,
                    ),
                    execution_token=execution_token,
                    intent=intent,
                )
        return None

    def _record_passive_receipt(
        self,
        owner: passive_protocol.PassiveOwner,
        result: OrderResult,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
    ) -> OrderStatus | None:
        """Bind the broker receipt to the mandate; None to continue.

        On any recognised receipt (incl. REJECTED/CANCELLED with zero fill)
        the outcome is ORDER_KNOWN — the authorisation stays consumed. A
        missing id or unknown status is UNCERTAIN with the facts preserved.
        A recording failure is itself UNCERTAIN (never success), escalated
        with the REAL risk/notifier collaborators (final-remediation
        finding 2: a direct UNKNOWN must pause, not just log).
        """
        broker_id = str(getattr(result, "broker_order_id", "") or "")
        status_text = str(getattr(result, "status", "") or "")
        # Phase2a W2/P3: the FIRST receipt includes the submit response's
        # ACTUAL executed quantity/price (an immediate FILLED carries real
        # fills here) so the overfill check sees the broker's own facts.
        executed_quantity = self._resolved_decimal(
            result, "executed_quantity", Decimal("0"),
        )
        executed_price = self._resolved_decimal(
            result, "executed_price", Decimal("0"),
        )
        classification = passive_protocol.classify_submit_receipt(
            broker_order_id=broker_id,
            status=status_text,
        )
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            return self._escalate_passive_uncertain(
                owner, broker_id,
                "passive submit hooks vanished mid-flight",
                risk=risk, notifier=notifier,
            )
        if classification == passive_protocol.SUBMIT_STATE_UNCERTAIN:
            return self._escalate_passive_uncertain(
                owner, broker_id,
                f"unrecognized broker receipt status {status_text!r}",
                risk=risk, notifier=notifier,
            )
        fact = passive_protocol.PassiveOutcomeFact(
            outcome=passive_protocol.SUBMIT_STATE_ORDER_KNOWN,
            broker_order_id=broker_id,
            broker_status=status_text,
            executed_quantity=(
                executed_quantity if executed_quantity > 0 else None
            ),
            executed_price=(
                executed_price if executed_price > 0 else None
            ),
        )
        try:
            write_result = hooks.record_outcome(owner, fact)
        except Exception as exc:
            return self._escalate_passive_uncertain(
                owner, broker_id,
                f"binding the broker receipt failed: {type(exc).__name__}",
                risk=risk, notifier=notifier,
            )
        # Phase2a W2: consume the W1 typed result. ESCALATED_UNCERTAIN
        # (e.g. an overfill above the immutable intent) is classified
        # here BUT returned only AFTER the caller has accounted the
        # actual broker fills — the escalate-return contractually happens
        # post-``_process_submitted_order`` (never an early return that
        # would skip booking the real fill). Returning this marker status
        # tells the submit path "recorded-uncertain; still process facts".
        escalated = self._passive_write_escalated(write_result)
        if escalated:
            # Run the FULL escalation side effects now (pause +
            # unresolved incident + uncertainty sink — the fact conflicts,
            # e.g. an actual overfill above the immutable intent), mark the
            # escalation in the per-call context, and return None: the
            # caller CONTINUES normal processing so the real broker fills
            # are accounted through the existing settlement path; the
            # dedicated entry's finalizer then surfaces the explicit
            # UNCERTAIN status from the marker.
            self._escalate_passive_uncertain(
                owner,
                broker_id,
                (
                    f"receipt escalated to uncertain (status {status_text!r},"
                    f" executed {executed_quantity}@{executed_price})"
                ),
                risk=risk,
                notifier=notifier,
            )
            self._active_execution_context[_PASSIVE_ESCALATED_KEY] = True
        return None

    @staticmethod
    def _passive_write_escalated(write_result: object) -> bool:
        """True when a record_outcome typed result means ESCALATED_UNCERTAIN.

        Fails closed: an unknown result value (future/None-typed
        implementations predating W1's enum) is treated as escalated —
        never as success.
        """
        enum_cls = getattr(passive_protocol, "OutcomeWriteResult", None)
        if enum_cls is None:
            # W1 enum not landed: no typed contract to trust — fail safe
            # by treating a non-None legacy return as needing no action,
            # but a None return (legacy success) stays success. Real
            # overfill safety then rests on the exception path plus the
            # fill-observation check below.
            return False
        try:
            return write_result is enum_cls.ESCALATED_UNCERTAIN
        except Exception:
            return True

    def _escalate_ownerless_passive_uncertain(
        self,
        *,
        reference: str,
        issue: str,
        broker_order_id: str | None,
        risk: RiskController | None,
        notifier: "NotifierInterface | None",
    ) -> OrderStatus:
        """B2: ownership-persistence fault with NO valid owner object.

        Never fabricates an owner (a fabricated intent cannot be
        constructed and would raise past the pause). Pauses with the
        ORDER_RECONCILIATION_UNCERTAIN prefix (non-auto), records an
        unresolved-reference incident carrying the REAL durable reference
        (raw claim token kept as an internal reference only), notifies,
        and returns an explicit UNCERTAIN status. Incident persistence
        failure remains an explicit uncertainty — it never masquerades as
        durable success.
        """
        reason = (
            f"{ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL} "
            f"{PASSIVE_LANE} attempt is UNCERTAIN: {issue} "
            f"(durable reference {reference[:8]}… retained)"
        )
        self._record_unresolved_reference_best_effort(
            reference,
            issue,
            broker_order_id=broker_order_id,
        )
        if risk is not None:
            try:
                if not risk.paused:
                    risk.pause(reason, auto_resumable=False)
            except Exception:
                logger.exception(
                    "failed to pause for an ownerless passive uncertainty"
                )
        if notifier is not None:
            try:
                notifier.notify_risk_event(
                    "PASSIVE_MANDATE_SUBMIT_UNCERTAIN",
                    reason,
                    severity="CRITICAL",
                )
            except Exception:
                logger.exception(
                    "failed to notify an ownerless passive uncertainty"
                )
        logger.critical(reason)
        return OrderStatus(
            broker_order_id or "", "UNCERTAIN", reason=reason,
        )

    def _escalate_passive_uncertain(
        self,
        owner: passive_protocol.PassiveOwner,
        broker_order_id: str,
        issue: str,
        *,
        risk: RiskController | None,
        notifier: "NotifierInterface | None",
    ) -> OrderStatus:
        """Durable uncertainty: pause, incident, explicit uncertain status."""
        reason = (
            f"{ORDER_RECONCILIATION_UNCERTAIN_PREFIX_LITERAL} "
            f"{PASSIVE_LANE} broker order "
            f"{broker_order_id or '<unknown>'} is UNCERTAIN: {issue}"
        )
        hooks = self._passive_hooks_or_none()
        if hooks is not None:
            try:
                hooks.record_outcome(
                    owner,
                    passive_protocol.PassiveOutcomeFact(
                        outcome=passive_protocol.SUBMIT_STATE_UNCERTAIN,
                        broker_order_id=broker_order_id,
                        reason=issue,
                    ),
                )
            except Exception:
                logger.exception(
                    "failed to persist the passive UNCERTAIN outcome",
                )
        # P2: the escalation must also leave a reconciliation-incident
        # record (the direct-UNKNOWN path previously paused and marked the
        # mandate but produced ZERO incidents). The REAL durable owner
        # reference and any known broker id travel with it.
        self._record_unresolved_reference_best_effort(
            _passive_owner_ref_string(owner),
            issue,
            broker_order_id=broker_order_id or None,
        )
        if risk is not None:
            try:
                # Phase2a W2: PRESERVE an existing pause reason — the
                # uncertainty adds its own incident/sink evidence without
                # overwriting whatever the operator or an earlier fault
                # already latched.
                if not risk.paused:
                    risk.pause(reason, auto_resumable=False)
            except Exception:
                logger.exception("failed to pause for uncertain passive submit")
        # Phase2a W2: notify the no-I/O uncertainty sink (the runner wires
        # it at startup — epoch-raise + quarantine — BEFORE any lane
        # gating, so it fires even with the flag OFF). A sink exception
        # must never erase the independent pause/incident above.
        if self._passive_uncertainty_sink is not None:
            try:
                self._passive_uncertainty_sink(
                    reason, broker_order_id or None,
                )
            except Exception:
                logger.exception(
                    "passive uncertainty sink failed (pause/incident "
                    "already applied)"
                )
        if notifier is not None:
            try:
                notifier.notify_risk_event(
                    "PASSIVE_MANDATE_SUBMIT_UNCERTAIN",
                    reason,
                    severity="CRITICAL",
                )
            except Exception:
                logger.exception("failed to notify uncertain passive submit")
        logger.critical(reason)
        return OrderStatus(broker_order_id, "UNCERTAIN", reason=reason)

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
        final_price_floor: Decimal | None = None
        # Phase2a W2 FINAL safety gate (parent correction #4): blocks
        # position-INCREASING requests whenever an external safety block is
        # active, even for a direct generic caller with no runner
        # entry-policy callback, and consults the AUTHORITATIVE reduction
        # quarantine for reductions (callback error is fail-closed). This
        # runs BEFORE the pre-submit boundary; an earlier optional check
        # can never replace it, and no new broker mutation exists here.
        external_block = getattr(risk, "external_block", None)
        external_block_fact = (
            external_block() if callable(external_block) else None
        )
        if (
            external_block_fact is not None
            and action in _ENTRY_ACTIONS
        ):
            block_source = str(getattr(external_block_fact, "source", ""))
            block_reason = str(getattr(external_block_fact, "reason", ""))
            return self._skip_order(
                symbol,
                action,
                (
                    f"external safety block active "
                    f"({block_source}: "
                    f"{block_reason}); new exposure refused"
                ),
                skip_category="RISK",
            )
        if (
            action in _POSITION_REDUCING_ACTIONS
            and self._passive_reduction_quarantine is not None
        ):
            try:
                quarantine_reason = self._passive_reduction_quarantine(
                    symbol,
                )
            except Exception as exc:
                return self._skip_order(
                    symbol,
                    action,
                    (
                        f"reduction quarantine check failed "
                        f"({type(exc).__name__}); fail-closed"
                    ),
                    skip_category="POSITION",
                )
            if quarantine_reason:
                return self._skip_order(
                    symbol,
                    action,
                    (
                        f"reduction refused: {quarantine_reason}"
                    ),
                    skip_category="POSITION",
                )
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
                final_price_floor = quote_check_result.price_floor
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
            if self._extended_hours_context is not None:
                extended_floor = price * (
                    Decimal("0.995") if action == "SELL" else Decimal("1.005")
                )
                if final_price_floor is None:
                    final_price_floor = extended_floor
                elif action == "SELL":
                    final_price_floor = max(final_price_floor, extended_floor)
                else:
                    final_price_floor = min(final_price_floor, extended_floor)
            if final_price_floor is not None:
                if not final_price_floor.is_finite() or final_price_floor <= 0:
                    return self._skip_order(
                        symbol,
                        action,
                        "reduce-only price floor must be finite and greater than zero",
                        skip_category="RISK",
                    )
                floor_tick = self._normalize_price_floor(symbol, action, final_price_floor)
                if floor_tick <= 0:
                    return self._skip_order(
                        symbol,
                        action,
                        "reduce-only price floor is below the minimum price tick",
                        skip_category="RISK",
                    )
                # Loss exits bypass fees, never the caller's execution price bound.
                if action == "SELL":
                    marketable_price = max(marketable_price, floor_tick)
                elif action == "BUY_TO_COVER":
                    marketable_price = min(marketable_price, floor_tick)
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
        approved_phase = self._approval_phase(symbol)
        outside_rth: str | None = None
        if self._extended_hours_context is not None:
            execution_market = market_for_symbol(symbol)
            now = datetime.now(timezone.utc)
            try:
                outside_rth_now = not is_trading_hours(execution_market, now)
            except TypeError:
                outside_rth_now = not is_trading_hours(execution_market)
            if outside_rth_now:
                if action in _ENTRY_ACTIONS:
                    # Extended-hours ENTRY final binding (flag-gated): re-check
                    # the executable phase at submit time — an approval made
                    # at 19:59 cannot submit at 20:01.
                    entry_decision = self._extended_hours_entry_decision(
                        symbol=symbol, market=execution_market, instant=now,
                    )
                    if entry_decision is None or not entry_decision.permitted:
                        return self._skip_order(
                            symbol, action,
                            "execution session closed before submission: "
                            + (entry_decision.reason if entry_decision else "extended-hours entries are not permitted"),
                            skip_category="SESSION",
                        )
                    outside_rth = outside_rth_for_phase(entry_decision.phase)
                    self._extended_hours_context = (
                        symbol, entry_decision.phase, now, "ENTRY",
                    )
                else:
                    decision = self.extended_hours_exit_decision(
                        action=action, symbol=symbol, market=execution_market,
                        reduce_only=reduce_only, instant=now,
                    )
                    if not decision.permitted:
                        return self._skip_order(
                            symbol, action,
                            f"execution session closed before submission: {decision.reason}",
                            skip_category="SESSION",
                        )
                    outside_rth = outside_rth_for_phase(decision.phase)
                    self._extended_hours_context = (
                        symbol, decision.phase, now, "EXIT",
                    )
        phase_refusal = self._phase_mismatch(
            symbol, action, market, approved_phase,
        )
        if phase_refusal is not None:
            return phase_refusal
        final_order = dataclass_replace(
            boundary_approval,
            price=(
                marketable_price
                if marketable_price is not None
                else boundary_approval.price
            ),
            protective_commit_required=protective_commit_required,
            outside_rth=outside_rth,
        )
        # SPY_PASSIVE submit right: consumed exactly once, after all runtime
        # and policy checks, before any broker mutation. A refusal BURNS the
        # authorisation (NO_SUBMIT) — never a reusable token. The boundary
        # itself (pre_submit_risk_check) never consumes the submit right, and
        # a duplicate boundary call cannot create one.
        passive_owner = self._active_passive_owner()
        if passive_owner is not None and final_order.action == "BUY":
            burn_reason = self._claim_passive_submission_right(final_order)
            if burn_reason is not None:
                self._record_passive_no_submit(
                    passive_owner, burn_reason, risk=risk,
                )
                return self._skip_order(
                    symbol,
                    action,
                    f"passive submit right refused: {burn_reason}",
                    skip_category="RISK",
                )
            recheck_issue = self._recheck_passive_before_broker_call(
                final_order, risk,
            )
            if recheck_issue is not None:
                self._record_passive_no_submit(
                    passive_owner, recheck_issue, risk=risk,
                )
                return self._skip_order(
                    symbol,
                    action,
                    f"passive post-CAS recheck refused: {recheck_issue}",
                    skip_category="RISK",
                )
        return final_order

    def _record_passive_no_submit(
        self,
        owner: passive_protocol.PassiveOwner,
        reason: str,
        *,
        risk: RiskController | None = None,
        notifier: "NotifierInterface | None" = None,
    ) -> None:
        """Burn the submit right to NO_SUBMIT; a recording failure is
        UNCERTAIN (never swallowed, final-remediation finding 2)."""
        hooks = self._passive_hooks_or_none()
        if hooks is None:
            logger.error(
                "passive submit hooks vanished before a NO_SUBMIT burn",
            )
            return
        try:
            hooks.record_outcome(
                owner,
                passive_protocol.PassiveOutcomeFact(
                    outcome=passive_protocol.SUBMIT_STATE_NO_SUBMIT,
                    reason=reason,
                ),
            )
        except Exception as exc:
            logger.exception(
                "failed to burn the passive submit right to NO_SUBMIT",
            )
            self._escalate_passive_uncertain(
                owner,
                "",
                f"recording the no-submit denial failed: {exc}",
                risk=risk,
                notifier=notifier,
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
        # Round-3 finding 1 (P0): the execution-initiator marker is
        # INTERNAL to the sizing/lane gates. It must never reach the
        # persisted ledger (ORDER_SUBMITTED payload_json, orders row,
        # audit, event-list API) — strip it here, where the durable
        # payload is assembled. Round-3 finding 2 (P1): when the
        # funded-margin exception was EFFECTIVE for this ENTRY, freeze
        # that verdict now and persist one small explicit evidence block
        # through the existing submission provenance; OFF/paper/unbound
        # and every non-entry order persist exactly what they do today.
        ledger_metadata.pop(EXECUTION_CONTEXT_INITIATOR_KEY, None)
        self._funded_margin_applied_at_submit = (
            action in _ENTRY_ACTIONS
            and self._funded_margin_exception_effective_for(symbol, action)
        )
        if self._funded_margin_applied_at_submit:
            ledger_metadata[FUNDED_MARGIN_EVIDENCE_KEY] = {
                "applied": True,
                "limiting_factor": self._funded_margin_last_limiting_factor,
            }
        _passive_owner_for_ledger = self._active_passive_owner()
        if _passive_owner_for_ledger is not None:
            # R1-5d: the trusted accounting/policy metadata must live in the
            # config_snapshot contract the existing record_order/reload
            # pipeline consumes — not only as a root-level marker.
            ledger_metadata["config_snapshot"] = (
                _passive_config_snapshot_json(_passive_owner_for_ledger)
            )
            ledger_metadata["accounting_fee_model"] = (
                ACCOUNTING_FEE_MODEL_US_SEC98
            )
            ledger_metadata["market"] = "US"
            ledger_metadata["passive_owner_ref"] = _passive_owner_ref_string(
                _passive_owner_for_ledger,
            )
        ledger_metadata.update({
            "submit_started_at": submit_started_at,
            "acknowledged_at": acknowledged_at,
            "ack_latency_ms": ack_latency_ms,
            "estimated_fee": float(
                abs(
                    _accounting_order_fee(
                        model=(
                            str(ledger_metadata["accounting_fee_model"])
                            if ledger_metadata.get("accounting_fee_model") is not None
                            else None
                        ),
                        market=str(ledger_metadata.get("market", "US")),
                        price=price,
                        quantity=qty,
                        legacy_rate=Decimal(
                            str(ledger_metadata.get("fee_rate", 0))
                        ),
                    )
                )
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
                    extended_hours=approved_order.outside_rth is not None,
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
            if approved_order.outside_rth is not None and order_status.status == "REJECTED":
                key = self._active_extended_hours_key()
                if key is not None:
                    # A plain REJECTED counts against its own kind. Only a
                    # broker outside_rth that is not ANY_TIME, or
                    # ExtendedHoursUnsupportedError, disables both.
                    self._extended_hours_terminal_outcome(
                        key,
                        unsupported=False,
                        notify_risk_event=notify_risk_event,
                    )
                    try:
                        if key[3] == "EXIT" and key in self._extended_hours_unsupported:
                            self._record_risk_event(
                                "extended-hours exits are disabled for "
                                f"{key[0]} {key[1]}; operator takeover required"
                            )
                        else:
                            self._record_risk_event(
                                f"extended-hours order {result.broker_order_id} "
                                "rejected"
                            )
                    except Exception:
                        logger.exception(
                            "extended-hours rejection risk event failed"
                        )
                if restore_engine_snapshot is not None and engine_snapshot is not None:
                    restore_engine_snapshot(engine_snapshot)
                return order_status
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
        extended_hours: bool = False,
    ) -> None:
        # Round-2 finding 3 / round-3 finding 2: the funded-margin verdict
        # is FROZEN at submit time by ``_process_submitted_order`` (which
        # runs first on every live path and resolves the exception while
        # the execution context is still live). Only ENTRY actions under
        # an effective exception carry it — the bounded cancel-retry path
        # is entry-scoped by construction.
        funded_margin_entry = (
            action in _ENTRY_ACTIONS
            and self._funded_margin_applied_at_submit
        )
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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
            ),
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
            submitted_at=time.monotonic(),
            restore_engine_snapshot_fn=restore_engine_snapshot_fn,
            extended_hours=extended_hours,
            extended_hours_key=self._active_extended_hours_key() if extended_hours else None,
            funded_margin_entry=funded_margin_entry,
        )
        passive_owner = self._active_passive_owner()
        if passive_owner is not None:
            # Force-overwrite the trusted accounting metadata for the
            # passive lane (never setdefault): US §9.8 is the only model,
            # the market is US, and the pending carries the COMPLETE owner
            # ref (mandate:claim:execution) so later callbacks survive the
            # cleared active context and rebuilds (R1-5).
            pending = dataclass_replace(
                pending,
                fee_model=ACCOUNTING_FEE_MODEL_US_SEC98,
                passive_owner_ref=_passive_owner_ref_string(passive_owner),
            )
            self._active_execution_context["market"] = "US"
            self._active_execution_context["accounting_fee_model"] = (
                ACCOUNTING_FEE_MODEL_US_SEC98
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
                    fee_model=(
                        pending.fee_model
                        if pending.fee_model
                        else existing_by_id.fee_model
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
                    extended_hours=existing_by_id.extended_hours or pending.extended_hours,
                    extended_hours_key=existing_by_id.extended_hours_key or pending.extended_hours_key,
                    extended_hours_cancel_requested=existing_by_id.extended_hours_cancel_requested,
                    funded_margin_entry=(
                        existing_by_id.funded_margin_entry
                        or pending.funded_margin_entry
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
            self._pending_entry_cancel_attempts.pop(order_id, None)

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
        # Round-2 finding 3 (P1): for a pending range ENTRY submitted while
        # the funded-margin exception was effective (stamped at submit
        # time — see _track_pending_order), the ONE-shot timeout latch
        # (``timeout_recovery_attempted``) is not enough: a failed first
        # cancel or a broker that keeps reporting live left the order live
        # into the cutoff/flatten windows. Drive BOUNDED cancel retries
        # from the real reconcile loop until a terminal status is
        # confirmed; once the cap is exhausted keep the order tracked
        # (existing uncertainty/pause semantics), record a risk event,
        # notify for manual intervention, and keep polling so a late
        # terminal status is still finalized through the normal path.
        if (
            self._order_status_timeout_seconds > 0
            and now - pending.submitted_at >= self._order_status_timeout_seconds
            and pending.timeout_recovery_attempted
            and pending.funded_margin_entry
        ):
            with self._state_lock:
                attempts = self._pending_entry_cancel_attempts.get(
                    pending.broker_order_id, 0,
                )
            if attempts < _PENDING_ENTRY_CANCEL_RETRY_CAP:
                self._handle_pending_order_timeout(
                    pending,
                    risk=risk,
                    notifier=notifier,
                    restore_engine_snapshot=restore_engine_snapshot,
                    notify_risk_event=notify_risk_event,
                )
                return
            if attempts == _PENDING_ENTRY_CANCEL_RETRY_CAP:
                # Cap reached without a confirmed terminal state:
                # escalate once for manual intervention, then fall
                # through to the ordinary status poll below (the order
                # stays tracked and blocks new entries; the risk pause
                # from the first timeout attempt remains).
                with self._state_lock:
                    self._pending_entry_cancel_attempts[
                        pending.broker_order_id
                    ] = attempts + 1
                reason = (
                    "PENDING_ENTRY_UNCONFIRMED: pending entry "
                    f"{pending.broker_order_id} for {pending.symbol} "
                    "could not be confirmed terminal after bounded "
                    f"cancel retries ({attempts}); manual intervention "
                    "required before the flatten window"
                )
                logger.error(reason)
                try:
                    self._record_risk_event(reason)
                except Exception:
                    logger.exception(
                        "failed to record pending-entry risk event for %s",
                        pending.broker_order_id,
                    )
                if notify_risk_event is not None:
                    try:
                        notify_risk_event(
                            "PENDING_ENTRY_UNCONFIRMED", reason,
                        )
                    except Exception:
                        logger.exception(
                            "failed to notify pending-entry uncertainty "
                            "for %s",
                            pending.broker_order_id,
                        )
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
            fee_model=pending.fee_model,
            next_status_check_at=now + self._order_status_poll_interval_seconds,
            submitted_at=pending.submitted_at,
            restore_engine_snapshot_fn=pending.restore_engine_snapshot_fn,
            timeout_recovery_attempted=pending.timeout_recovery_attempted,
            extended_hours=pending.extended_hours,
            extended_hours_key=pending.extended_hours_key,
            extended_hours_cancel_requested=pending.extended_hours_cancel_requested,
            # R1-5: the complete owner reference survives every rebuild.
            passive_owner_ref=pending.passive_owner_ref,
            # Round-2 finding 3: the submit-time funded-margin verdict is
            # immutable pending data — it survives every rebuild.
            funded_margin_entry=pending.funded_margin_entry,
        )
        with self._state_lock:
            self._pending_orders_by_id[updated_pending.broker_order_id] = updated_pending
            self._rebuild_pending_orders_by_symbol_locked()
            self._pending_status_query_warned_ids.discard(updated_pending.broker_order_id)

        # R1-5c: a passive mandate tracks same-id receipt PROGRESS here,
        # from the durable pending owner ref (never the cleared context).
        self._record_passive_receipt_progress(
            updated_pending,
            order_status,
            risk=risk,
            notify_risk_event=notify_risk_event,
        )

        status_persisted = self._safe_update_order_status_from_result(order_status)
        status = order_status.status
        if status in {"FILLED", *_FAILED_ORDER_STATUSES} and not status_persisted:
            self._pause_for_order_status_persistence_failure(
                updated_pending,
                status,
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            self._escalate_passive_pending_uncertain(
                updated_pending,
                f"order status persistence failed for terminal {status}",
                risk=risk,
                notify_risk_event=notify_risk_event,
            )
            return
        if status == "FILLED":
            try:
                self._finalize_pending_fill(updated_pending, order_status, risk=risk, notifier=notifier, notify_risk_event=notify_risk_event)
            except Exception as exc:
                # R1-5: a settlement failure on a passive mandate must
                # surface as mandate UNCERTAINTY too, never only a risk
                # pause while the mandate stays success-like.
                self._escalate_passive_pending_uncertain(
                    updated_pending,
                    f"settlement raised {type(exc).__name__}: {exc}",
                    risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                raise
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
            if updated_pending.extended_hours and updated_pending.extended_hours_key is not None:
                self._extended_hours_terminal_outcome(
                    updated_pending.extended_hours_key,
                    unsupported=False,
                    notify_risk_event=notify_risk_event,
                )
            else:
                self._pause_after_failed_order(updated_pending.broker_order_id, status, risk, notify_risk_event)
            self._clear_pending_order(updated_pending.broker_order_id)
            if effective_restore is not None and updated_pending.engine_snapshot is not None:
                effective_restore(updated_pending.engine_snapshot)
            return
        if updated_pending.extended_hours_key is not None:
            expected_outside_rth = outside_rth_for_phase(
                updated_pending.extended_hours_key[1],
            )
        else:
            expected_outside_rth = "ANY_TIME"
        if (
            updated_pending.extended_hours
            and order_status.outside_rth != expected_outside_rth
            and not updated_pending.extended_hours_cancel_requested
        ):
            if updated_pending.extended_hours_key is not None:
                self._extended_hours_terminal_outcome(
                    updated_pending.extended_hours_key,
                    unsupported=True,
                    notify_risk_event=notify_risk_event,
                )
            self._handle_pending_order_timeout(
                dataclass_replace(updated_pending, extended_hours_cancel_requested=True),
                risk=risk, notifier=notifier,
                restore_engine_snapshot=restore_engine_snapshot, notify_risk_event=notify_risk_event,
            )
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
        # Round-2 finding 3: count this cancel attempt for the bounded
        # entry-retry path driven by the reconcile loop.
        with self._state_lock:
            self._pending_entry_cancel_attempts[
                pending.broker_order_id
            ] = (
                self._pending_entry_cancel_attempts.get(
                    pending.broker_order_id, 0,
                )
                + 1
            )
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
                elif pending.extended_hours and pending.extended_hours_key is not None:
                    self._extended_hours_terminal_outcome(
                        pending.extended_hours_key,
                        unsupported=False,
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
                elif pending.extended_hours and pending.extended_hours_key is not None:
                    self._extended_hours_terminal_outcome(
                        pending.extended_hours_key,
                        unsupported=False,
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
                        if pending.extended_hours and pending.extended_hours_key is not None:
                            self._extended_hours_terminal_outcome(
                                pending.extended_hours_key,
                                unsupported=False,
                                notify_risk_event=notify_risk_event,
                            )
                        else:
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
                self._book_fill(pending, order_status, risk=risk, fill_qty=fill_qty,
                                notify_risk_event=notify_risk_event)
                self._record_passive_fill_observation(
                    pending, order_status,
                    risk=risk, notifier=notifier,
                    notify_risk_event=notify_risk_event,
                )
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
            with self._state_lock:
                self._finalized_order_ids.add(order_id)
        finally:
            with self._state_lock:
                self._fill_finalization_in_flight.discard(order_id)

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
        receipt = self._book_fill(
            pending, order_status, risk=risk, fill_qty=fill_qty,
            notify_risk_event=notify_risk_event,
        )
        # Phase2a W2: observe the accounted fill on a MARKED passive order
        # (immediate and delayed fills both land here). The booking above
        # already happened — this never trims or re-books it.
        self._record_passive_fill_observation(
            pending, order_status,
            risk=risk, notifier=notifier,
            notify_risk_event=notify_risk_event,
        )
        key = settlement_key(pending.broker_order_id)
        with self._state_lock:
            if key is not None and key in self._fill_tail_completed:
                return
            facts = receipt.intent.facts
            self._safe_notify_order(
                notifier, facts.action, facts.symbol, str(facts.quantity),
                str(facts.price), pending.broker_order_id,
            )
            self._mark_fill_processed(facts.symbol, facts.action)
            if facts.action in _POSITION_REDUCING_ACTIONS:
                self._notify_reduction_fill(facts.symbol, facts.action, facts.quantity)
            if key is not None:
                self._fill_tail_completed.add(key)

    def _plan_authoritative_exit_outcome(
        self,
        pending: _PendingOrder,
        order_status: object,
        *,
        fill_price: Decimal,
        fill_qty: Decimal,
        fallback_avg_price: Decimal,
    ) -> tuple[Decimal | None, Mapping[str, float | str | datetime | None]]:
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
            return None, {}

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
        fee_model = pending.fee_model or None
        market = market_for_symbol(pending.symbol)
        entry_fee = _accounting_allocated_entry_fee(
            model=fee_model,
            market=market,
            cost_basis_price=cost_basis_price,
            position_quantity_before=position_quantity_before,
            fill_quantity=fill_qty,
            legacy_rate=fee_rate,
        )
        actual_fee_raw = getattr(order_status, "actual_fee", None)
        actual_exit_fee: Decimal | None = None
        if actual_fee_raw is not None:
            try:
                candidate = Decimal(str(actual_fee_raw))
                # 0.00 is the broker's settling placeholder (a paper account
                # reports it with no fee items), not proof of a free exit, so
                # it is charged like an unreported fee rather than booked
                # into risk as zero.
                if candidate.is_finite() and candidate > 0:
                    actual_exit_fee = candidate
            except Exception:
                actual_exit_fee = None
        if actual_exit_fee is None:
            exit_fee = _accounting_order_fee(
                model=fee_model,
                market=market,
                price=fill_price,
                quantity=fill_qty,
                legacy_rate=fee_rate,
            )
            pnl_fee_source = "ESTIMATED"
        else:
            exit_fee = actual_exit_fee
            pnl_fee_source = "MIXED"
        pnl_fee = entry_fee + exit_fee
        net_pnl = gross_pnl - pnl_fee
        metadata: dict[str, float | str | datetime | None] = {
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
        return net_pnl, metadata

    def _settle_fill_in_process(self, intent: SettlementIntent) -> SettlementReceipt:
        key = settlement_key(intent.broker_order_id)
        stored = self._settlement_receipts.get(key) if key is not None else None
        if stored is not None:
            comparison = compare_repeat(stored.intent.facts, intent.facts)
            if comparison.verdict is RepeatVerdict.CONFLICT:
                raise SettlementConflictError(intent.broker_order_id, comparison.reason)
            return dataclass_replace(stored, is_new=False)
        if intent.metadata:
            persisted = self._safe_update_order_status(
                intent.broker_order_id, intent.terminal_status, intent.filled_at,
                float(intent.facts.quantity), float(intent.facts.price), dict(intent.metadata),
            )
            if not persisted:
                raise OrderPersistenceError(
                    f"failed to persist authoritative accounting for order {intent.broker_order_id}"
                )
        if intent.persist_position and self._persist_entry is not None:
            try:
                self._persist_entry(intent.facts.symbol, intent.quantity_after, intent.cost_after)
            except Exception as exc:
                position_kind = "reduction" if intent.facts.action in _POSITION_REDUCING_ACTIONS else "entry"
                raise OrderPersistenceError(
                    f"failed to persist tracked {position_kind} for {intent.facts.symbol}"
                ) from exc
        receipt = SettlementReceipt(intent, is_new=True)
        if key is not None:
            self._settlement_receipts[key] = receipt
        return receipt

    def _book_fill(
        self,
        pending: _PendingOrder,
        order_status: OrderStatus,
        *,
        risk: RiskController | None = None,
        fill_qty: Decimal | None = None,
        notify_risk_event: _NotifyRiskEvent | None = None,
    ) -> SettlementReceipt:
        key = settlement_key(pending.broker_order_id)
        if key is None:
            logger.warning("fill without broker order ID cannot be settled idempotently: %s %s",
                           pending.symbol, pending.action)
        with self._state_lock:
            quantity = fill_qty if fill_qty is not None else self._resolved_decimal(
                order_status, "executed_quantity", pending.quantity,
            )
            price = self._resolved_decimal(order_status, "executed_price", pending.price)
            facts = FillFacts(
                pending.symbol, pending.action, quantity, price,
                "BROKER" if OrderStatus._positive(order_status.executed_quantity) > 0 else "FALLBACK",
                "BROKER" if OrderStatus._positive(order_status.executed_price) > 0 else "FALLBACK",
            )
            stored = self._settlement_receipts.get(key) if key is not None else None
            if stored is not None:
                intent = dataclass_replace(stored.intent, facts=facts, terminal_status=order_status.status)
            else:
                entry = self._entry_positions.get(pending.symbol)
                current_quantity = entry.quantity if entry is not None else Decimal("0")
                current_cost = entry.cost if entry is not None else Decimal("0")
                opened_at = entry.opened_at if entry is not None else None
                side = entry.side if entry is not None else "LONG"
                net_pnl: Decimal | None = None
                metadata: Mapping[str, float | str | datetime | None] = {}
                persist_position = False
                quantity_after, cost_after = current_quantity, current_cost
                if pending.action in _ENTRY_ACTIONS and quantity > 0 and price > 0:
                    booking = plan_entry_booking(current_quantity, current_cost, quantity, price)
                    quantity_after, cost_after = booking.quantity_after, booking.cost_after
                    side = "SHORT" if pending.action == "SELL_SHORT" else "LONG"
                    opened_at = opened_at or datetime.now(timezone.utc)
                    persist_position = True
                elif pending.action in _POSITION_REDUCING_ACTIONS and quantity > 0:
                    reduction = plan_reduction_booking(current_quantity, current_cost, quantity)
                    quantity_after, cost_after = reduction.quantity_after, reduction.cost_after
                    persist_position = current_quantity > 0
                    avg_price = self._resolve_avg_price_for_exit(pending.symbol, pending.avg_price, quantity)
                    net_pnl, metadata = self._plan_authoritative_exit_outcome(
                        pending, order_status, fill_price=price, fill_qty=quantity,
                        fallback_avg_price=avg_price,
                    )
                # Reconcile paths hand us lightweight status objects that carry
                # only the fields they needed, so read both defensively rather
                # than assuming the full OrderStatus shape.
                intent = SettlementIntent(
                    pending.broker_order_id,
                    facts,
                    str(getattr(order_status, "status", "FILLED") or "FILLED"),
                    getattr(order_status, "broker_updated_at", None)
                    or datetime.now(timezone.utc),
                    quantity_after, cost_after, side, opened_at, persist_position, net_pnl, metadata,
                )
            try:
                receipt = self._settle_fill(intent)
            except Exception:
                self._pause_for_order_status_persistence_failure(
                    pending, "FILLED_ACCOUNTING_UNCERTAIN", risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                # R1-5: a settlement failure on a passive mandate must also
                # surface in the mandate row — never leave it success-like.
                self._escalate_passive_pending_uncertain(
                    pending,
                    "settlement raised during fill finalization",
                    risk=risk,
                    notify_risk_event=notify_risk_event,
                )
                raise
            booked = receipt.intent
            if key is not None:
                self._settlement_receipts[key] = receipt
            if booked.net_pnl is not None and risk is not None:
                if key is None:
                    risk.record_trade(float(booked.net_pnl))
                else:
                    risk.consume_settlement(key, float(booked.net_pnl))
                drawdown_reason = risk.consume_drawdown_limit_reason()
                if drawdown_reason is not None:
                    try:
                        self._record_risk_event(drawdown_reason, "DRAWDOWN_LIMIT")
                    except Exception:
                        logger.exception("failed to record drawdown limit risk event for %s", pending.symbol)
                    if notify_risk_event is not None:
                        try:
                            notify_risk_event("DRAWDOWN_LIMIT", drawdown_reason)
                        except Exception:
                            logger.exception("failed to send drawdown limit notification for %s", pending.symbol)
            if key is None or key not in self._settlement_memory_applied:
                if booked.persist_position:
                    if booked.quantity_after <= 0:
                        self._entry_positions.pop(booked.facts.symbol, None)
                    else:
                        self._entry_positions[booked.facts.symbol] = _TrackedEntry(
                            booked.quantity_after, booked.cost_after, booked.side, booked.opened_at,
                        )
                if key is not None:
                    self._settlement_memory_applied.add(key)
            return receipt

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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
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
            fee_model=str(
                self._active_execution_context.get("accounting_fee_model", "") or ""
            ),
            next_status_check_at=time.monotonic() + self._order_status_poll_interval_seconds,
            submitted_at=time.monotonic(),
            restore_engine_snapshot_fn=restore_engine_snapshot,
            # Round-3 finding 2: the submission record failed, but the
            # frozen submit-time verdict survives in memory — the order
            # keeps its bounded-retry entitlement on the reconcile path.
            funded_margin_entry=(
                resolved_action in _ENTRY_ACTIONS
                and self._funded_margin_applied_at_submit
            ),
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
            outside_rth=str(getattr(result, "outside_rth", "") or ""),
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
            outside_rth=str(getattr(result, "outside_rth", "") or ""),
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
