from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol, cast

from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import get_session, is_trading_hours
from app.models import OpeningMomentumExecution, OrderRecord
from app.schemas import (
    OpeningMomentumExecutionConfigResponse,
    OpeningMomentumExecutionResponse,
    OpeningMomentumExecutionStatusResponse,
)
from app.services.opening_momentum_shadow_service import (
    CandleProvider,
    OpeningMomentumExecutionSignal,
    OpeningMomentumShadowService,
)


_ACTIVE_STATUSES = frozenset({
    "ARMED",
    "SUBMITTING",
    "SUBMITTED",
    "OPEN",
    "EXITING",
    "UNCERTAIN",
})
_ENTRY_ACTIONS = frozenset({"BUY"})
_EXIT_ACTIONS = frozenset({"SELL"})
_RESERVATION_LEAD_MINUTES = 2
_RESERVATION_MINUTES = 5


def opening_execution_reservation_window(
    now: datetime | None = None,
) -> bool:
    """Reserve the single capital slot while the opening signal forms."""
    if not settings.opening_momentum_execution_enabled:
        return False
    current = _as_utc(now or datetime.now(timezone.utc))
    session = get_session("US")
    local = session.local(current)
    if local.weekday() >= 5:
        return False
    session_open = datetime.combine(
        local.date(),
        session.rth_open,
        tzinfo=session.timezone,
    ).astimezone(timezone.utc)
    return (
        session_open - timedelta(minutes=_RESERVATION_LEAD_MINUTES)
        <= current
        < session_open + timedelta(minutes=_RESERVATION_MINUTES)
    )


class OpeningExecutionRunner(Protocol):
    def execute_opening_momentum_entry(
        self,
        *,
        execution_id: int,
        symbol: str,
        reference_entry_price: float,
        entry_deadline_at: datetime,
        max_price_deviation_bps: float,
        stop_loss_pct: float,
        max_holding_minutes: int,
        signal_context: dict[str, object],
    ) -> dict[str, object]: ...

    def refresh_opening_execution_registry(
        self,
        db: Session | None = None,
    ) -> None: ...


class OpeningMomentumExecutionService:
    """Run one causal opening entry through the durable live-order pipeline."""

    def __init__(
        self,
        db: Session,
        candle_provider: CandleProvider | None = None,
        runner: OpeningExecutionRunner | None = None,
    ) -> None:
        self.db = db
        self.candle_provider = candle_provider
        self.runner = runner

    def tick(
        self,
        *,
        now: datetime | None = None,
    ) -> OpeningMomentumExecutionStatusResponse:
        explicit_now = now is not None
        current = _as_utc(now or datetime.now(timezone.utc))
        self._reconcile_orders(current)
        if not settings.opening_momentum_execution_enabled:
            self._refresh_runner_registry()
            return self.get_status()
        if not is_trading_hours("US", current):
            self._refresh_runner_registry()
            return self.get_status()

        session = get_session("US")
        session_date = session.local(current).date()
        identity = self._execution_identity()
        if (
            identity.forward_evidence_start_date is not None
            and session_date < identity.forward_evidence_start_date
        ):
            self._refresh_runner_registry()
            return self.get_status()
        row = self._execution_for_session(session_date)
        if row is None:
            if self.active_policies(self.db):
                self._refresh_runner_registry()
                return self.get_status()
            _, _, entry_deadline_at = self._session_entry_schedule(
                identity,
                session_date=session_date,
            )
            if current > entry_deadline_at:
                variant = self._execution_variant()
                row = self._record_missed_session(
                    variant or identity,
                    variant=variant,
                    session_date=session_date,
                    observed_at=current,
                )
            else:
                signal = OpeningMomentumShadowService(
                    self.db,
                    self.candle_provider,
                ).evaluate_execution_signal(now=current)
                if signal is None:
                    self._refresh_runner_registry()
                    return self.get_status()
                row = self._record_signal(signal, armed_at=current)
            self.db.commit()
            self._refresh_runner_registry()
            if not explicit_now:
                current = datetime.now(timezone.utc)

        if row.status == "ARMED":
            if current > _as_utc(row.entry_deadline_at):
                row.status = "EXPIRED"
                row.reason = "ENTRY_WINDOW_EXPIRED"
                row.updated_at = current
                self.db.commit()
                self._refresh_runner_registry()
            elif current >= _as_utc(row.entry_due_at):
                self._submit(
                    row,
                    now=current,
                    use_wall_clock=not explicit_now,
                )
        return self.get_status()

    def get_status(self) -> OpeningMomentumExecutionStatusResponse:
        variant = self._execution_variant()
        identity = variant or self._execution_identity()
        universe = variant.symbols if variant is not None else ()
        required_symbols = (
            variant.required_symbols
            if variant is not None
            else identity.required_symbols
        )
        excluded_symbols = (
            variant.excluded_symbols
            if variant is not None
            else identity.excluded_symbols
        )
        universe_ready = self._variant_universe_ready(
            identity,
            variant,
        )
        latest = (
            self.db.query(OpeningMomentumExecution)
            .filter(
                OpeningMomentumExecution.config_version
                == identity.config_version
            )
            .order_by(
                OpeningMomentumExecution.session_date.desc(),
                OpeningMomentumExecution.id.desc(),
            )
            .first()
        )
        enabled = settings.opening_momentum_execution_enabled
        order_submission_allowed = bool(
            enabled
            and settings.opening_momentum_execution_paper_confirmed
            and settings.full_buying_power_usage_enabled
            and universe_ready
        )
        state = (
            str(latest.status)
            if latest is not None
            else "WAITING"
            if enabled
            else "DISABLED"
        )
        return OpeningMomentumExecutionStatusResponse(
            config=OpeningMomentumExecutionConfigResponse(
                enabled=enabled,
                paper_account_confirmed=(
                    settings.opening_momentum_execution_paper_confirmed
                ),
                order_submission_allowed=order_submission_allowed,
                algorithm_version=identity.algorithm_version,
                config_version=identity.config_version,
                universe_source=(
                    variant.universe_source
                    if variant is not None
                    else "NONE"
                ),
                selection_run_id=(
                    variant.selection_run_id
                    if variant is not None
                    else None
                ),
                universe_size=len(universe),
                universe=list(universe),
                required_symbols=list(required_symbols),
                excluded_symbols=list(excluded_symbols),
                universe_ready=universe_ready,
                signal_minutes=identity.decision_config.signal_minutes,
                execution_delay_minutes=(
                    identity.decision_config.execution_delay_minutes
                ),
                holding_minutes=identity.decision_config.holding_minutes,
                stop_loss_pct=float(
                    identity.decision_config.stop_loss_pct or 0
                ),
                minimum_path_efficiency=(
                    identity.minimum_path_efficiency
                ),
                maximum_market_return_bps=(
                    identity.maximum_market_return_bps
                ),
                exceptional_minimum_path_efficiency=(
                    identity.exceptional_minimum_path_efficiency
                ),
                exceptional_maximum_market_return_bps=(
                    identity.exceptional_maximum_market_return_bps
                ),
                forward_evidence_start_date=(
                    identity.forward_evidence_start_date
                ),
                max_entry_delay_seconds=(
                    settings.opening_momentum_execution_max_entry_delay_seconds
                ),
                max_price_deviation_bps=(
                    settings.opening_momentum_execution_max_price_deviation_bps
                ),
            ),
            state=cast(Any, state),
            latest=(self._response(latest) if latest is not None else None),
        )

    def list_executions(
        self,
        *,
        limit: int = 100,
    ) -> list[OpeningMomentumExecutionResponse]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        rows = (
            self.db.query(OpeningMomentumExecution)
            .order_by(
                OpeningMomentumExecution.session_date.desc(),
                OpeningMomentumExecution.id.desc(),
            )
            .limit(limit)
            .all()
        )
        return [self._response(row) for row in rows]

    @staticmethod
    def active_policies(
        db: Session,
    ) -> list[OpeningMomentumExecution]:
        return (
            db.query(OpeningMomentumExecution)
            .filter(OpeningMomentumExecution.status.in_(_ACTIVE_STATUSES))
            .order_by(OpeningMomentumExecution.id.asc())
            .all()
        )

    @classmethod
    def reconcile_fill(
        cls,
        db: Session,
        *,
        symbol: str,
        action: str,
    ) -> None:
        normalized_action = str(action or "").upper()
        if normalized_action not in (_ENTRY_ACTIONS | _EXIT_ACTIONS):
            return
        row = (
            db.query(OpeningMomentumExecution)
            .filter(
                OpeningMomentumExecution.symbol == symbol,
                OpeningMomentumExecution.status.in_(_ACTIVE_STATUSES),
            )
            .order_by(OpeningMomentumExecution.id.desc())
            .first()
        )
        if row is None:
            return
        cls._reconcile_record_from_orders(db, row)

    @staticmethod
    def mark_exiting(
        db: Session,
        *,
        symbol: str,
        reason: str,
    ) -> None:
        row = (
            db.query(OpeningMomentumExecution)
            .filter(
                OpeningMomentumExecution.symbol == symbol,
                OpeningMomentumExecution.status.in_({"OPEN", "SUBMITTED"}),
            )
            .order_by(OpeningMomentumExecution.id.desc())
            .first()
        )
        if row is None:
            return
        row.status = "EXITING"
        row.reason = reason
        row.updated_at = datetime.now(timezone.utc)
        db.add(row)

    def _execution_identity(self):
        return OpeningMomentumShadowService(
            self.db
        ).paper_execution_variant_identity()

    def _execution_variant(self):
        return OpeningMomentumShadowService(
            self.db
        ).paper_execution_variant()

    def _execution_for_session(
        self,
        session_date: date,
    ) -> OpeningMomentumExecution | None:
        return (
            self.db.query(OpeningMomentumExecution)
            .filter(
                OpeningMomentumExecution.session_date == session_date,
            )
            .order_by(OpeningMomentumExecution.id.desc())
            .first()
        )

    @staticmethod
    def _session_entry_schedule(
        identity: Any,
        *,
        session_date: date,
    ) -> tuple[datetime, datetime, datetime]:
        session = get_session("US")
        session_open = datetime.combine(
            session_date,
            session.rth_open,
            tzinfo=session.timezone,
        ).astimezone(timezone.utc)
        config = identity.decision_config
        signal_at = session_open + timedelta(
            minutes=config.signal_minutes - 1,
        )
        entry_due_at = session_open + timedelta(
            minutes=(
                config.signal_minutes
                + config.execution_delay_minutes
            ),
        )
        entry_deadline_at = entry_due_at + timedelta(
            seconds=(
                settings
                .opening_momentum_execution_max_entry_delay_seconds
            ),
        )
        return signal_at, entry_due_at, entry_deadline_at

    @staticmethod
    def _variant_universe_ready(
        identity: Any,
        variant: Any | None,
    ) -> bool:
        if variant is None or variant.universe_source == "NONE":
            return False
        universe = tuple(variant.symbols)
        return bool(
            len(universe)
            >= identity.decision_config.minimum_universe_size
            and set(variant.required_symbols).issubset(universe)
            and not set(variant.excluded_symbols).intersection(universe)
        )

    def _record_missed_session(
        self,
        identity: Any,
        *,
        variant: Any | None,
        session_date: date,
        observed_at: datetime,
    ) -> OpeningMomentumExecution:
        signal_at, entry_due_at, entry_deadline_at = (
            self._session_entry_schedule(
                identity,
                session_date=session_date,
            )
        )
        universe = tuple(variant.symbols) if variant is not None else ()
        universe_ready = self._variant_universe_ready(identity, variant)
        reason = (
            "ENTRY_WINDOW_MISSED"
            if universe_ready
            else "PREOPEN_UNIVERSE_UNAVAILABLE"
        )
        signal = OpeningMomentumExecutionSignal(
            session_date=session_date,
            algorithm_version=identity.algorithm_version,
            config_version=identity.config_version,
            universe_source=(
                variant.universe_source
                if variant is not None
                else "NONE"
            ),
            selection_run_id=(
                variant.selection_run_id
                if variant is not None
                else None
            ),
            action="SKIP",
            reason=reason,
            symbol=None,
            signal_at=signal_at,
            entry_due_at=entry_due_at,
            universe_size=len(universe),
            market_return_bps=None,
            candidate_return_bps=None,
            excess_return_bps=None,
            reference_entry_price=None,
            stop_loss_pct=float(
                identity.decision_config.stop_loss_pct or 0
            ),
            max_holding_minutes=(
                identity.decision_config.holding_minutes
            ),
            context={
                "entry_window_missed": True,
                "observed_at": observed_at.isoformat(),
                "universe_ready": universe_ready,
                "universe": list(universe),
                "entry_deadline_at": entry_deadline_at.isoformat(),
            },
        )
        row = self._record_signal(signal, armed_at=observed_at)
        row.status = "EXPIRED"
        return row

    def _record_signal(
        self,
        signal: OpeningMomentumExecutionSignal,
        *,
        armed_at: datetime,
    ) -> OpeningMomentumExecution:
        deadline = signal.entry_due_at + timedelta(
            seconds=(
                settings.opening_momentum_execution_max_entry_delay_seconds
            )
        )
        row = OpeningMomentumExecution(
            session_date=signal.session_date,
            algorithm_version=signal.algorithm_version,
            config_version=signal.config_version,
            universe_source=signal.universe_source,
            selection_run_id=signal.selection_run_id,
            status=("ARMED" if signal.action == "ENTER_LONG" else "SKIPPED"),
            reason=signal.reason,
            symbol=signal.symbol,
            signal_at=signal.signal_at,
            armed_at=armed_at,
            entry_due_at=signal.entry_due_at,
            entry_deadline_at=deadline,
            universe_size=signal.universe_size,
            market_return_bps=signal.market_return_bps,
            candidate_return_bps=signal.candidate_return_bps,
            excess_return_bps=signal.excess_return_bps,
            reference_entry_price=signal.reference_entry_price,
            max_price_deviation_bps=(
                settings.opening_momentum_execution_max_price_deviation_bps
            ),
            stop_loss_pct=signal.stop_loss_pct,
            max_holding_minutes=signal.max_holding_minutes,
            signal_context_json=json.dumps(
                signal.context,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def _submit(
        self,
        row: OpeningMomentumExecution,
        *,
        now: datetime,
        use_wall_clock: bool,
    ) -> None:
        if self.runner is None:
            row.status = "FAILED"
            row.reason = "RUNNER_UNAVAILABLE"
            row.updated_at = now
            self.db.commit()
            return
        if row.symbol is None or row.reference_entry_price is None:
            row.status = "FAILED"
            row.reason = "INVALID_ARMED_SIGNAL"
            row.updated_at = now
            self.db.commit()
            return

        row.status = "SUBMITTING"
        row.requested_at = now
        row.submit_attempts += 1
        row.updated_at = now
        self.db.commit()
        self._refresh_runner_registry()
        try:
            result = self.runner.execute_opening_momentum_entry(
                execution_id=row.id,
                symbol=row.symbol,
                reference_entry_price=row.reference_entry_price,
                entry_deadline_at=row.entry_deadline_at,
                max_price_deviation_bps=row.max_price_deviation_bps,
                stop_loss_pct=row.stop_loss_pct,
                max_holding_minutes=row.max_holding_minutes,
                signal_context=_json_object(row.signal_context_json),
            )
        except Exception as exc:
            completed_at = (
                datetime.now(timezone.utc) if use_wall_clock else now
            )
            row.status = "UNCERTAIN"
            row.reason = f"ORDER_SUBMISSION_UNCERTAIN:{type(exc).__name__}"
            row.updated_at = completed_at
            self.db.commit()
            self._refresh_runner_registry()
            return

        completed_at = (
            datetime.now(timezone.utc) if use_wall_clock else now
        )
        status = str(result.get("status") or "FAILED").upper()
        order_id = str(result.get("order_id") or "")
        reason = str(result.get("reason") or status)
        row.entry_order_id = order_id
        row.reason = reason
        if bool(result.get("executed")) and order_id:
            row.status = "OPEN" if status == "FILLED" else "SUBMITTED"
        elif status in {
            "BUSY",
            "CAPITAL_SLOT_BUSY",
            "NO_QUOTE",
            "QUOTE_DEVIATION",
        } and (
            completed_at <= _as_utc(row.entry_deadline_at)
        ):
            row.status = "ARMED"
        elif "UNCERTAIN" in status:
            row.status = "UNCERTAIN"
        elif status == "ENTRY_WINDOW_EXPIRED":
            row.status = "EXPIRED"
        elif status in {"REJECTED", "CANCELLED", "SKIPPED"}:
            row.status = "REJECTED"
        else:
            row.status = "FAILED"
        row.updated_at = completed_at
        self.db.commit()
        self._reconcile_orders(completed_at)
        self._refresh_runner_registry()

    def _reconcile_orders(self, now: datetime) -> None:
        rows = (
            self.db.query(OpeningMomentumExecution)
            .filter(OpeningMomentumExecution.status.in_(_ACTIVE_STATUSES))
            .order_by(OpeningMomentumExecution.id.asc())
            .all()
        )
        changed = False
        for row in rows:
            before = (
                row.status,
                row.entry_order_id,
                row.exit_order_id,
                row.entry_price,
                row.exit_price,
            )
            self._reconcile_record_from_orders(self.db, row)
            if (
                row.status == "ARMED"
                and now > _as_utc(row.entry_deadline_at)
            ):
                row.status = "EXPIRED"
                row.reason = "ENTRY_WINDOW_EXPIRED"
                row.updated_at = now
            elif (
                row.status == "SUBMITTING"
                and now
                > _as_utc(row.entry_deadline_at) + timedelta(seconds=60)
            ):
                row.status = "UNCERTAIN"
                row.reason = "SUBMISSION_RESULT_UNAVAILABLE"
                row.updated_at = now
            after = (
                row.status,
                row.entry_order_id,
                row.exit_order_id,
                row.entry_price,
                row.exit_price,
            )
            changed = changed or before != after
        if changed:
            self.db.commit()

    @classmethod
    def _reconcile_record_from_orders(
        cls,
        db: Session,
        row: OpeningMomentumExecution,
    ) -> None:
        entry = cls._linked_order(
            db,
            row,
            actions=_ENTRY_ACTIONS,
            broker_order_id=row.entry_order_id,
        )
        if entry is None:
            return
        row.entry_order_id = str(entry.broker_order_id or "")
        entry_quantity = float(entry.executed_quantity or 0)
        if entry_quantity <= 0:
            if str(entry.status or "").upper() in {"REJECTED", "CANCELLED"}:
                row.status = "REJECTED"
                row.reason = f"ENTRY_{str(entry.status).upper()}"
            return
        row.entry_filled_at = entry.filled_at or entry.created_at
        row.entry_price = float(entry.executed_price or entry.price or 0)
        row.quantity = entry_quantity
        if row.status not in {"EXITING", "CLOSED"}:
            row.status = "OPEN"
            row.reason = "ENTRY_FILLED"

        exit_candidates = (
            db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == row.symbol,
                OrderRecord.side.in_(_EXIT_ACTIONS),
                OrderRecord.executed_quantity.is_not(None),
                OrderRecord.executed_quantity > 0,
                OrderRecord.created_at >= (row.entry_filled_at or row.armed_at),
            )
            .order_by(OrderRecord.created_at.asc(), OrderRecord.id.asc())
            .all()
        )
        exits = [
            item
            for item in exit_candidates
            if cls._belongs_to_execution(item, row)
        ]
        if not exits:
            return
        exited_quantity = sum(
            float(item.executed_quantity or 0) for item in exits
        )
        latest = exits[-1]
        row.exit_order_id = str(latest.broker_order_id or "")
        if exited_quantity + 1e-9 < entry_quantity:
            row.status = "EXITING"
            row.reason = "PARTIAL_EXIT_FILLED"
            return
        row.status = "CLOSED"
        row.reason = str(latest.exit_cause or "EXIT_FILLED")
        row.exit_filled_at = latest.filled_at or latest.created_at
        row.exit_price = float(latest.executed_price or latest.price or 0)
        pnl_values = [item.net_pnl for item in exits]
        row.net_pnl = (
            sum(float(value) for value in pnl_values if value is not None)
            if all(value is not None for value in pnl_values)
            else None
        )
        row.updated_at = datetime.now(timezone.utc)

    @staticmethod
    def _linked_order(
        db: Session,
        row: OpeningMomentumExecution,
        *,
        actions: frozenset[str],
        broker_order_id: str,
    ) -> OrderRecord | None:
        if broker_order_id:
            candidate = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == broker_order_id)
                .order_by(OrderRecord.id.desc())
                .first()
            )
            if (
                candidate is not None
                and candidate.symbol == row.symbol
                and str(candidate.side or "").upper() in actions
                and OpeningMomentumExecutionService._belongs_to_execution(
                    candidate,
                    row,
                )
            ):
                return candidate
            return None
        candidates = (
            db.query(OrderRecord)
            .filter(
                OrderRecord.symbol == row.symbol,
                OrderRecord.side.in_(actions),
                OrderRecord.created_at >= row.armed_at,
            )
            .order_by(OrderRecord.created_at.desc(), OrderRecord.id.desc())
            .all()
        )
        for candidate in candidates:
            if OpeningMomentumExecutionService._belongs_to_execution(
                candidate,
                row,
            ):
                return candidate
        return None

    @staticmethod
    def _belongs_to_execution(
        order: OrderRecord,
        row: OpeningMomentumExecution,
    ) -> bool:
        snapshot = _json_object(order.config_snapshot)
        execution_signal = snapshot.get("execution_signal")
        return bool(
            isinstance(execution_signal, dict)
            and execution_signal.get("opening_execution_id") == row.id
        )

    def _refresh_runner_registry(self) -> None:
        if self.runner is None:
            return
        # Lend the tick's own session: the cron holds it across the whole
        # tick, and the runner opening a second one inside the refresh was
        # the 2026-09-04 live re-entrancy violation.
        self.runner.refresh_opening_execution_registry(db=self.db)

    @staticmethod
    def _response(
        row: OpeningMomentumExecution,
    ) -> OpeningMomentumExecutionResponse:
        signal_context = _json_object(row.signal_context_json)
        return OpeningMomentumExecutionResponse(
            id=row.id,
            session_date=row.session_date,
            algorithm_version=row.algorithm_version,
            config_version=row.config_version,
            universe_source=row.universe_source,
            selection_run_id=row.selection_run_id,
            status=cast(Any, row.status),
            reason=row.reason,
            symbol=row.symbol,
            signal_at=row.signal_at,
            armed_at=row.armed_at,
            entry_due_at=row.entry_due_at,
            entry_deadline_at=row.entry_deadline_at,
            requested_at=row.requested_at,
            universe_size=row.universe_size,
            market_return_bps=row.market_return_bps,
            candidate_return_bps=row.candidate_return_bps,
            excess_return_bps=row.excess_return_bps,
            candidate_path_efficiency=_nonnegative_context_float(
                signal_context,
                "candidate_path_efficiency",
            ),
            candidate_signal_turnover=_nonnegative_context_float(
                signal_context,
                "candidate_signal_turnover",
            ),
            candidate_avg_dollar_volume=_nonnegative_context_float(
                signal_context,
                "candidate_avg_dollar_volume",
            ),
            candidate_signal_turnover_ratio=(
                _nonnegative_context_float(
                    signal_context,
                    "candidate_signal_turnover_ratio",
                )
            ),
            reference_entry_price=row.reference_entry_price,
            max_price_deviation_bps=row.max_price_deviation_bps,
            stop_loss_pct=row.stop_loss_pct,
            max_holding_minutes=row.max_holding_minutes,
            signal_context=signal_context,
            submit_attempts=row.submit_attempts,
            entry_order_id=row.entry_order_id,
            exit_order_id=row.exit_order_id,
            entry_filled_at=row.entry_filled_at,
            entry_price=row.entry_price,
            quantity=row.quantity,
            exit_filled_at=row.exit_filled_at,
            exit_price=row.exit_price,
            net_pnl=row.net_pnl,
        )


def _json_object(value: object) -> dict[str, object]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _nonnegative_context_float(
    context: dict[str, object],
    key: str,
) -> float | None:
    value = context.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) and parsed >= 0 else None


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)
