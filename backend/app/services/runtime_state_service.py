from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController

logger = logging.getLogger("auto_trade.runtime_state")

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class RuntimeStateService:
    def load(self, db: Any, engine: StrategyEngine, risk: RiskController) -> None:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        config = svc.get_config()
        state = svc.get_runtime_state()

        engine.params = StrategyParams(
            symbol=config.symbol,
            market=config.market,
            buy_low=config.buy_low,
            sell_high=config.sell_high,
            short_selling=config.short_selling,
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
        risk.kill_switch = state.kill_switch
        risk.paused = state.paused

    def persist(self, db: Any, engine: StrategyEngine, risk: RiskController) -> None:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        svc.update_runtime_state(
            engine_state=engine.state.value,
            last_price=engine.last_price,
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            kill_switch=risk.kill_switch,
            paused=risk.paused,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
        )

    def persist_risk(self, db: Any, risk: RiskController) -> None:
        from app.services.strategy_service import StrategyService

        svc = StrategyService(db)
        svc.update_runtime_state(
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
        )

    def record_risk_event(self, db: Any, reason: str) -> None:
        from app.models import RiskEvent

        event = RiskEvent(event_type="RISK_REJECTION", reason=reason)
        db.add(event)
        db.commit()

    def _coerce_engine_state(self, value: object) -> EngineState:
        try:
            return EngineState(value)
        except (TypeError, ValueError):
            logger.warning("invalid engine state %r in DB, defaulting to FLAT", value)
            return EngineState.FLAT
