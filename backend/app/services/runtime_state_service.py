from __future__ import annotations

import logging
import math
from datetime import date, datetime, time, timezone
from typing import TYPE_CHECKING, Any

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController
from app.config import settings

logger = logging.getLogger("auto_trade.runtime_state")


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
        )
        risk.daily_pnl = state.daily_pnl
        risk.consecutive_losses = state.consecutive_losses
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
        from app.models import RuntimeState, RuntimeStateSnapshot

        primary_symbol = (engine.params.symbol or "").strip().upper()
        runtime_state = db.query(RuntimeState).filter(
            RuntimeState.symbol == primary_symbol
        ).first()
        if runtime_state is None:
            runtime_state = RuntimeState(symbol=primary_symbol)
        runtime_state.engine_state = engine.state.value
        runtime_state.last_price = engine.last_price
        runtime_state.daily_pnl = risk.daily_pnl
        runtime_state.daily_pnl_date = risk.daily_pnl_date
        runtime_state.consecutive_losses = risk.consecutive_losses
        runtime_state.kill_switch = risk.kill_switch
        runtime_state.paused = risk.paused
        runtime_state.pause_reason = risk.pause_reason
        runtime_state.paused_at = risk.paused_at
        runtime_state.pause_auto_resumable = risk.pause_auto_resumable
        runtime_state.last_trigger_price = engine.last_trigger_price
        runtime_state.last_trigger_at = engine.last_trigger_at
        runtime_state.long_entry_rearm_required = engine.long_entry_rearm_required
        runtime_state.updated_at = datetime.now(timezone.utc)
        db.add(runtime_state)
        db.add(
            RuntimeStateSnapshot(
                symbol=primary_symbol,
                engine_state=engine.state.value,
                paused=risk.paused,
                kill_switch=risk.kill_switch,
                daily_pnl=risk.daily_pnl,
                consecutive_losses=risk.consecutive_losses,
                last_price=engine.last_price,
                last_trigger_price=engine.last_trigger_price,
                execution_state=runtime_state.execution_state or "IDLE",
                reduction_reason=runtime_state.reduction_reason or "",
            )
        )

    def persist_risk(self, db: Any, risk: RiskController, *, symbol: str = "") -> None:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        svc.update_runtime_state(
            symbol=(symbol or "").strip().upper(),
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            daily_pnl_date=risk.daily_pnl_date,
        )

    def record_risk_event(self, db: Any, reason: str) -> None:
        from app.models import RiskEvent

        event = RiskEvent(event_type="RISK_REJECTION", reason=reason)
        db.add(event)
        db.commit()

    def load_symbol_runtime(self, db: Any, engine: StrategyEngine, symbol: str) -> None:
        from app.services.strategy_service import StrategyService

        state = StrategyService(db).get_runtime_state(symbol=symbol)
        engine.state = self._coerce_engine_state(state.engine_state)
        engine.last_price = state.last_price
        engine.last_trigger_price = state.last_trigger_price
        engine.last_trigger_at = state.last_trigger_at
        engine.restore_long_entry_rearm(
            bool(getattr(state, "long_entry_rearm_required", False))
        )

    def persist_symbol(self, db: Any, engine: StrategyEngine, symbol: str | None = None, risk: RiskController | None = None) -> None:
        from app.services.strategy_service import StrategyService

        runtime_symbol = (symbol if symbol is not None else engine.params.symbol or "").strip().upper()
        StrategyService(db).update_runtime_state(
            symbol=runtime_symbol,
            engine_state=engine.state.value,
            last_price=engine.last_price,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
            long_entry_rearm_required=engine.long_entry_rearm_required,
        )
        self.record_snapshot(db, engine, risk or RiskController(), symbol=runtime_symbol)

    def record_snapshot(self, db: Any, engine: StrategyEngine, risk: RiskController, *, symbol: str = "") -> None:
        from app.models import RuntimeStateSnapshot
        from app.services.strategy_service import StrategyService

        runtime_state = StrategyService(db).get_runtime_state(symbol=symbol)

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
        )
        db.add(snapshot)
        db.commit()

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
