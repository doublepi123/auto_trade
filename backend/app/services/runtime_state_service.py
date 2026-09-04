from __future__ import annotations

import logging
import math
import time as time_mod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController
from app.config import settings

logger = logging.getLogger("auto_trade.runtime_state")


_PRICE_SNAPSHOT_INTERVAL = timedelta(minutes=1)
_SNAPSHOT_FLOAT_TOLERANCE = 1e-9


@dataclass(frozen=True)
class RuntimeStateWrite:
    """One planned ``runtime_state`` change, ready to issue as batched DML.

    Planning is separated from writing so a caller can end its read phase
    before the first write statement. Everything is captured as plain
    parameters, so applying it touches no ORM row the read-ending commit
    expired and rows of one shape can go out as a single executemany.

    ``changed`` is False when the persisted row already holds exactly these
    values, which makes the write a provable no-op worth skipping: the single
    SQLite writer is shared with the research jobs.
    """

    symbol: str
    values: dict[str, Any]
    row_id: int | None
    changed: bool
    snapshot_values: dict[str, Any] | None


def _batched_by_shape(
    rows: list[dict[str, Any]],
) -> list[list[dict[str, Any]]]:
    """Group parameter dicts so each group is one executemany statement."""
    batches: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for row in rows:
        batches.setdefault(tuple(sorted(row)), []).append(row)
    return list(batches.values())



def hard_ceiling_float(value: object, hard_value: float) -> float:
    """Return a positive finite value no less restrictive than the hard cap."""
    try:
        candidate = float(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return hard_value
    if not math.isfinite(candidate) or candidate <= 0:
        return hard_value
    return min(candidate, hard_value)


def hard_ceiling_int(value: object, hard_value: int) -> int:
    try:
        candidate = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return hard_value
    if candidate <= 0:
        return hard_value
    return min(candidate, hard_value)


def hard_floor_int(value: object, hard_value: int) -> int:
    try:
        candidate = int(value)  # pyright: ignore[reportArgumentType]
    except (TypeError, ValueError, OverflowError):
        return hard_value
    if candidate <= 0:
        return hard_value
    return max(candidate, hard_value)


class RuntimeStateService:
    def __init__(self) -> None:
        self._last_snapshot_log_at: float = 0.0
        self._last_snapshot_state: tuple = ()
    def load(self, db: Any, engine: StrategyEngine, risk: RiskController) -> Any:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        config = svc.get_config()
        state = svc.get_primary_runtime_state()

        engine.params = StrategyParams(
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
        engine.state = self._coerce_engine_state(state.engine_state)
        engine.last_price = state.last_price
        engine.last_trigger_price = state.last_trigger_price
        engine.last_trigger_at = state.last_trigger_at
        engine.restore_long_entry_rearm(
            bool(getattr(state, "long_entry_rearm_required", False))
        )

        risk.config = RiskConfig(
            max_daily_loss=config.max_daily_loss,
            max_consecutive_losses=config.max_consecutive_losses,
            max_drawdown_amount=getattr(config, "max_drawdown_amount", None),
        )
        risk.daily_pnl = state.daily_pnl
        risk.consecutive_losses = state.consecutive_losses
        risk.restore_drawdown_state(
            cumulative_realized_pnl=float(
                getattr(state, "cumulative_realized_pnl", 0.0) or 0.0
            ),
            peak_realized_pnl=float(
                getattr(state, "peak_realized_pnl", 0.0) or 0.0
            ),
        )
        risk.begin_day(persisted_date=_coerce_date(state.daily_pnl_date))
        risk.kill_switch = state.kill_switch
        risk.restore_pause(
            paused=state.paused,
            reason=state.pause_reason or "",
            paused_at=_coerce_datetime(state.paused_at),
            auto_resumable=state.pause_auto_resumable,
        )
        return config

    def persist(self, db: Any, engine: StrategyEngine, risk: RiskController) -> None:
        self.stage(db, engine, risk)
        db.commit()

    def stage(self, db: Any, engine: StrategyEngine, risk: RiskController) -> None:
        """Stage runtime state and its history snapshot without committing."""
        row, write = self._plan_primary(db, engine, risk)
        self._stage_write(db, row, write)

    def plan(
        self,
        db: Any,
        engine: StrategyEngine,
        risk: RiskController,
    ) -> RuntimeStateWrite:
        """Decide the primary runtime write without staging it."""
        return self._plan_primary(db, engine, risk)[1]

    def _plan_primary(
        self,
        db: Any,
        engine: StrategyEngine,
        risk: RiskController,
    ) -> tuple[Any | None, RuntimeStateWrite]:
        from app.models import RuntimeState, RuntimeStateSnapshot

        primary_symbol = (engine.params.symbol or "").strip().upper()
        row = db.query(RuntimeState).filter(
            RuntimeState.symbol == primary_symbol
        ).first()
        captured_at = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "engine_state": engine.state.value,
            "last_price": engine.last_price,
            "daily_pnl": risk.daily_pnl,
            "daily_pnl_date": risk.daily_pnl_date,
            "consecutive_losses": risk.consecutive_losses,
            "cumulative_realized_pnl": risk.cumulative_realized_pnl,
            "peak_realized_pnl": risk.peak_realized_pnl,
            "kill_switch": risk.kill_switch,
            "paused": risk.paused,
            "pause_reason": risk.pause_reason,
            "paused_at": risk.paused_at,
            "pause_auto_resumable": risk.pause_auto_resumable,
            "last_trigger_price": engine.last_trigger_price,
            "last_trigger_at": engine.last_trigger_at,
            "long_entry_rearm_required": engine.long_entry_rearm_required,
            "updated_at": captured_at,
        }
        snapshot = self._snapshot_if_needed(
            db,
            RuntimeStateSnapshot(
                symbol=primary_symbol,
                engine_state=engine.state.value,
                paused=risk.paused,
                kill_switch=risk.kill_switch,
                daily_pnl=risk.daily_pnl,
                consecutive_losses=risk.consecutive_losses,
                last_price=engine.last_price,
                last_trigger_price=engine.last_trigger_price,
                execution_state=getattr(row, "execution_state", "") or "IDLE",
                reduction_reason=getattr(row, "reduction_reason", "") or "",
                created_at=captured_at,
            ),
        )
        return row, self._build_write(row, primary_symbol, values, snapshot)

    def _build_write(
        self,
        row: Any | None,
        symbol: str,
        values: dict[str, Any],
        snapshot: Any | None,
    ) -> RuntimeStateWrite:
        return RuntimeStateWrite(
            symbol=symbol,
            values=values,
            row_id=None if row is None else int(row.id),
            changed=self._row_differs(row, values),
            snapshot_values=(
                None if snapshot is None else self._snapshot_params(snapshot)
            ),
        )

    @staticmethod
    def _row_differs(row: Any | None, values: dict[str, Any]) -> bool:
        """True when the persisted row does not already hold these values.

        ``updated_at`` is excluded: it is the write-time clock, so including
        it would mark every row dirty forever and defeat the check.
        """
        if row is None:
            return True
        return any(
            getattr(row, column, None) != value
            for column, value in values.items()
            if column != "updated_at"
        )

    @staticmethod
    def _snapshot_params(snapshot: Any) -> dict[str, Any]:
        from sqlalchemy import inspect as sa_inspect

        return {
            column.key: getattr(snapshot, column.key)
            for column in sa_inspect(type(snapshot)).mapper.column_attrs
            if column.key != "id"
        }

    def persist_risk(self, db: Any, risk: RiskController, *, symbol: str = "") -> None:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        svc.update_runtime_state(
            symbol=(symbol or "").strip().upper(),
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            cumulative_realized_pnl=risk.cumulative_realized_pnl,
            peak_realized_pnl=risk.peak_realized_pnl,
            daily_pnl_date=risk.daily_pnl_date,
        )

    def record_risk_event(
        self,
        db: Any,
        reason: str,
        event_type: str = "RISK_REJECTION",
    ) -> None:
        from app.models import RiskEvent

        event = RiskEvent(event_type=event_type, reason=reason)
        db.add(event)
        db.commit()

    def load_symbol_runtime(self, db: Any, engine: StrategyEngine, symbol: str) -> None:
        """Read one secondary runtime's persisted state, transaction-neutral.

        Reached from ``_sync_symbol_runtimes`` on a BORROWED session (the
        opening-momentum cron's, during a registry refresh), so this must not
        commit, roll back, flush or close: the lifetime and outcome of the
        caller's transaction belong to the caller. A missing row therefore
        returns without touching the engine instead of get-or-creating one --
        the old ``get_runtime_state`` path committed the caller's transaction
        whenever the row was absent, finalizing writes the caller had not
        committed. A fresh in-memory engine already holds exactly what a
        fresh row would have loaded, so nothing is lost by skipping.
        """
        from app.models import RuntimeState

        normalized = (symbol or "").strip().upper()
        state = (
            db.query(RuntimeState)
            .filter(RuntimeState.symbol == normalized)
            .first()
        )
        if state is None:
            return
        engine.state = self._coerce_engine_state(state.engine_state)
        engine.last_price = state.last_price
        engine.last_trigger_price = state.last_trigger_price
        engine.last_trigger_at = state.last_trigger_at
        engine.restore_long_entry_rearm(
            bool(getattr(state, "long_entry_rearm_required", False))
        )

    def persist_symbol(self, db: Any, engine: StrategyEngine, symbol: str | None = None, risk: RiskController | None = None) -> None:
        self.stage_symbol(db, engine, symbol=symbol, risk=risk)
        db.commit()

    def stage_symbol(
        self,
        db: Any,
        engine: StrategyEngine,
        symbol: str | None = None,
        risk: RiskController | None = None,
    ) -> None:
        """Stage one secondary runtime for a caller-managed transaction."""
        row, write = self._plan_symbol(db, engine, symbol, risk)
        self._stage_write(db, row, write)

    def plan_symbol(
        self,
        db: Any,
        engine: StrategyEngine,
        symbol: str | None = None,
        risk: RiskController | None = None,
    ) -> RuntimeStateWrite:
        """Decide one secondary runtime's write without staging it."""
        return self._plan_symbol(db, engine, symbol, risk)[1]

    def _plan_symbol(
        self,
        db: Any,
        engine: StrategyEngine,
        symbol: str | None,
        risk: RiskController | None,
    ) -> tuple[Any | None, RuntimeStateWrite]:
        from app.models import RuntimeState, RuntimeStateSnapshot

        runtime_symbol = (symbol if symbol is not None else engine.params.symbol or "").strip().upper()
        row = db.query(RuntimeState).filter(
            RuntimeState.symbol == runtime_symbol
        ).first()
        captured_at = datetime.now(timezone.utc)
        values: dict[str, Any] = {
            "engine_state": engine.state.value,
            "last_price": engine.last_price,
            "last_trigger_price": engine.last_trigger_price,
            "last_trigger_at": engine.last_trigger_at,
            "long_entry_rearm_required": engine.long_entry_rearm_required,
            "updated_at": captured_at,
        }

        snapshot_risk = risk or RiskController()
        snapshot = self._snapshot_if_needed(
            db,
            RuntimeStateSnapshot(
                symbol=runtime_symbol,
                engine_state=engine.state.value,
                paused=snapshot_risk.paused,
                kill_switch=snapshot_risk.kill_switch,
                daily_pnl=snapshot_risk.daily_pnl,
                consecutive_losses=snapshot_risk.consecutive_losses,
                last_price=engine.last_price,
                last_trigger_price=engine.last_trigger_price,
                execution_state=getattr(row, "execution_state", "") or "IDLE",
                reduction_reason=getattr(row, "reduction_reason", "") or "",
                created_at=captured_at,
            ),
        )
        return row, self._build_write(row, runtime_symbol, values, snapshot)

    @staticmethod
    def _stage_write(
        db: Any,
        row: Any | None,
        write: RuntimeStateWrite,
    ) -> None:
        from app.models import RuntimeState, RuntimeStateSnapshot

        target = row if row is not None else RuntimeState(symbol=write.symbol)
        for column, value in write.values.items():
            setattr(target, column, value)
        db.add(target)
        if write.snapshot_values is not None:
            db.add(RuntimeStateSnapshot(**write.snapshot_values))

    @staticmethod
    def apply_writes(db: Any, writes: Sequence[RuntimeStateWrite]) -> None:
        """Issue planned writes as batched DML in a fresh transaction.

        Every statement is DML, so the transaction never upgrades a read
        snapshot -- SQLite refuses that immediately instead of honouring
        busy_timeout. Rows sharing a parameter shape go out as one
        executemany, so the single writer is held for a constant number of
        round-trips rather than one per row; the per-row form doubled writer
        hold time and halved completed passes against the research cohort.
        """
        from sqlalchemy import insert, update

        from app.models import RuntimeState, RuntimeStateSnapshot

        updates = [
            {"id": write.row_id, **write.values}
            for write in writes
            if write.row_id is not None
        ]
        inserts = [
            {"symbol": write.symbol, **write.values}
            for write in writes
            if write.row_id is None
        ]
        snapshots = [
            write.snapshot_values
            for write in writes
            if write.snapshot_values is not None
        ]
        for batch in _batched_by_shape(updates):
            db.execute(update(RuntimeState), batch)
        for batch in _batched_by_shape(inserts):
            db.execute(insert(RuntimeState), batch)
        for batch in _batched_by_shape(snapshots):
            db.execute(insert(RuntimeStateSnapshot), batch)

    def record_snapshot(self, db: Any, engine: StrategyEngine, risk: RiskController, *, symbol: str = "") -> None:
        from app.models import RuntimeStateSnapshot
        from app.services.strategy_service import StrategyService

        runtime_state = StrategyService(db).get_runtime_state(symbol=symbol)
        captured_at = datetime.now(timezone.utc)

        snapshot = RuntimeStateSnapshot(
            symbol=symbol,
            engine_state=engine.state.value,
            paused=risk.paused,
            kill_switch=risk.kill_switch,
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            last_price=engine.last_price,
            last_trigger_price=engine.last_trigger_price,
            execution_state=getattr(runtime_state, "execution_state", "IDLE"),
            reduction_reason=getattr(runtime_state, "reduction_reason", ""),
            created_at=captured_at,
        )
        self._stage_snapshot_if_needed(db, snapshot)
        db.commit()

        now = time_mod.monotonic()
        current_state = (
            engine.state.value,
            risk.paused,
            risk.kill_switch,
            risk.daily_pnl,
            risk.consecutive_losses,
            getattr(runtime_state, "execution_state", "IDLE"),
            getattr(runtime_state, "reconciliation_gate", ""),
        )
        if (
            current_state != self._last_snapshot_state
            or now - self._last_snapshot_log_at >= 300.0
        ):
            self._last_snapshot_log_at = now
            self._last_snapshot_state = current_state
            logger.info(
                "reconciliation snapshot restored: engine=%s paused=%s kill=%s "
                "pnl=%.2f losses=%d exec=%s gate=%s symbol=%s",
                engine.state.value,
                risk.paused,
                risk.kill_switch,
                risk.daily_pnl,
                risk.consecutive_losses,
                getattr(runtime_state, "execution_state", "IDLE"),
                getattr(runtime_state, "reconciliation_gate", ""),
                (symbol or "primary"),
            )

    @classmethod
    def _stage_snapshot_if_needed(
        cls,
        db: Any,
        candidate: Any,
    ) -> bool:
        snapshot = cls._snapshot_if_needed(db, candidate)
        if snapshot is None:
            return False
        db.add(snapshot)
        return True

    @classmethod
    def _snapshot_if_needed(
        cls,
        db: Any,
        candidate: Any,
    ) -> Any | None:
        """Keep state transitions immediately and coalesce quote-only churn."""
        from app.models import RuntimeStateSnapshot

        latest = (
            db.query(RuntimeStateSnapshot)
            .filter(RuntimeStateSnapshot.symbol == candidate.symbol)
            .order_by(RuntimeStateSnapshot.id.desc())
            .first()
        )
        if not cls._should_record_snapshot(latest, candidate):
            return None
        return candidate

    @classmethod
    def _should_record_snapshot(
        cls,
        latest: Any | None,
        candidate: Any,
    ) -> bool:
        if latest is None:
            return True
        if (
            latest.engine_state != candidate.engine_state
            or latest.paused != candidate.paused
            or latest.kill_switch != candidate.kill_switch
            or not cls._same_snapshot_number(
                latest.daily_pnl,
                candidate.daily_pnl,
            )
            or latest.consecutive_losses != candidate.consecutive_losses
            or not cls._same_snapshot_number(
                latest.last_trigger_price,
                candidate.last_trigger_price,
            )
            or latest.execution_state != candidate.execution_state
            or latest.reduction_reason != candidate.reduction_reason
        ):
            return True
        if cls._same_snapshot_number(latest.last_price, candidate.last_price):
            return False
        latest_at = _coerce_datetime(latest.created_at)
        candidate_at = _coerce_datetime(candidate.created_at)
        if latest_at is None or candidate_at is None:
            return True
        return candidate_at - latest_at >= _PRICE_SNAPSHOT_INTERVAL

    @staticmethod
    def _same_snapshot_number(left: Any, right: Any) -> bool:
        try:
            return math.isclose(
                float(left),
                float(right),
                rel_tol=0.0,
                abs_tol=_SNAPSHOT_FLOAT_TOLERANCE,
            )
        except (TypeError, ValueError, OverflowError):
            return left == right

    def load_reduction(self, db: Any, *, symbol: str) -> dict[str, Any] | None:
        from app.services.strategy_service import StrategyService

        state = StrategyService(db).get_runtime_state(symbol=symbol)
        if getattr(state, "execution_state", "IDLE") != "REDUCING":
            return None
        return {
            "action": getattr(state, "reduction_action", ""),
            "cause": getattr(state, "reduction_cause", ""),
            "reason": getattr(state, "reduction_reason", ""),
            "started_at": getattr(state, "reduction_started_at", None),
            "trigger_price": getattr(state, "reduction_trigger_price", None),
        }

    def persist_reduction(
        self,
        db: Any,
        *,
        symbol: str,
        action: str,
        cause: str,
        reason: str,
        started_at: datetime,
        trigger_price: float,
    ) -> None:
        from app.services.strategy_service import StrategyService

        StrategyService(db).update_runtime_state(
            symbol=symbol,
            execution_state="REDUCING",
            reduction_action=action,
            reduction_cause=cause,
            reduction_reason=reason,
            reduction_started_at=started_at,
            reduction_trigger_price=trigger_price,
        )

    def clear_reduction(self, db: Any, *, symbol: str) -> None:
        from app.services.strategy_service import StrategyService

        StrategyService(db).update_runtime_state(
            symbol=symbol,
            execution_state="IDLE",
            reduction_action="",
            reduction_cause="",
            reduction_reason="",
            reduction_started_at=None,
            reduction_trigger_price=None,
        )

    def query_history(
        self,
        db: Any,
        *,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        limit: int = 200,
        symbol: str = "",
        include_legacy_empty: bool = False,
    ) -> list[Any]:
        from app.models import RuntimeStateSnapshot
        from sqlalchemy import or_

        normalized_symbol = (symbol or "").strip().upper()
        query = db.query(RuntimeStateSnapshot)
        if normalized_symbol:
            if include_legacy_empty:
                query = query.filter(
                    or_(
                        RuntimeStateSnapshot.symbol == normalized_symbol,
                        RuntimeStateSnapshot.symbol == "",
                    )
                )
            else:
                query = query.filter(RuntimeStateSnapshot.symbol == normalized_symbol)
        else:
            query = query.filter(RuntimeStateSnapshot.symbol == "")
        if start_at is not None:
            query = query.filter(RuntimeStateSnapshot.created_at >= start_at)
        if end_at is not None:
            query = query.filter(RuntimeStateSnapshot.created_at <= end_at)
        rows = (
            query.order_by(RuntimeStateSnapshot.created_at.desc(), RuntimeStateSnapshot.id.desc())
            .limit(limit)
            .all()
        )
        return list(reversed(rows))

    def _coerce_engine_state(self, value: object) -> EngineState:
        try:
            return EngineState(value)
        except (TypeError, ValueError):
            logger.warning("invalid engine state %r in DB, defaulting to FLAT", value)
            return EngineState.FLAT


def _coerce_date(value: object) -> date | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
        except (ValueError, TypeError):
            return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value
    if isinstance(value, date):
        return datetime.combine(value, time(0, 0, 0), tzinfo=timezone.utc)
    return None
