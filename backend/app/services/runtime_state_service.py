from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Any

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController

logger = logging.getLogger("auto_trade.runtime_state")

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
            short_selling=config.short_selling,
            min_profit_amount=config.min_profit_amount,
            auto_resume_minutes=config.auto_resume_minutes,
            fee_rate_us=config.fee_rate_us,
            fee_rate_hk=config.fee_rate_hk,
            min_repricing_pct=config.min_repricing_pct,
            llm_action_cooldown_seconds=config.llm_action_cooldown_seconds,
        )
        engine.state = self._coerce_engine_state(state.engine_state)
        engine.last_price = state.last_price
        engine.last_trigger_price = state.last_trigger_price
        engine.last_trigger_at = state.last_trigger_at

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
        from app.services.strategy_service import StrategyService

        primary_symbol = (engine.params.symbol or "").strip().upper()
        svc = StrategyService(db)
        svc.update_runtime_state(
            symbol=primary_symbol,
            engine_state=engine.state.value,
            last_price=engine.last_price,
            daily_pnl=risk.daily_pnl,
            daily_pnl_date=risk.daily_pnl_date,
            consecutive_losses=risk.consecutive_losses,
            kill_switch=risk.kill_switch,
            paused=risk.paused,
            pause_reason=risk.pause_reason,
            paused_at=risk.paused_at,
            pause_auto_resumable=risk.pause_auto_resumable,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
        )
        self.record_snapshot(db, engine, risk, symbol=primary_symbol)

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

    def persist_symbol(self, db: Any, engine: StrategyEngine, symbol: str | None = None) -> None:
        from app.services.strategy_service import StrategyService

        runtime_symbol = (symbol if symbol is not None else engine.params.symbol or "").strip().upper()
        StrategyService(db).update_runtime_state(
            symbol=runtime_symbol,
            engine_state=engine.state.value,
            last_price=engine.last_price,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
        )
        self.record_snapshot(db, engine, RiskController(), symbol=runtime_symbol)

    def record_snapshot(self, db: Any, engine: StrategyEngine, risk: RiskController, *, symbol: str = "") -> None:
        from app.models import RuntimeStateSnapshot

        snapshot = RuntimeStateSnapshot(
            symbol=symbol,
            engine_state=engine.state.value,
            paused=risk.paused,
            kill_switch=risk.kill_switch,
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            last_price=engine.last_price,
            last_trigger_price=engine.last_trigger_price,
        )
        db.add(snapshot)
        db.commit()

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
    if isinstance(value, date):
        return value
    return None


def _coerce_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value
