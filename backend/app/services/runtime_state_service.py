from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.core.engine import EngineState, StrategyEngine, StrategyParams
from app.core.risk import RiskConfig, RiskController

logger = logging.getLogger("auto_trade.runtime_state_service")


class RuntimeStateConfig(Protocol):
    symbol: str
    market: str
    buy_low: float
    sell_high: float
    short_selling: bool
    max_daily_loss: float
    max_consecutive_losses: int


class RuntimeStateSnapshot(Protocol):
    engine_state: str
    last_price: float
    last_trigger_price: float
    last_trigger_at: datetime | None
    daily_pnl: float
    consecutive_losses: int
    kill_switch: bool
    paused: bool


class RuntimeStateStore(Protocol):
    def get_config(self) -> RuntimeStateConfig:
        ...

    def get_runtime_state(self) -> RuntimeStateSnapshot:
        ...

    def update_runtime_state(self, **kwargs: object) -> object:
        ...


@dataclass(frozen=True)
class RuntimeStateSnapshotValues:
    engine_state: str
    last_price: float
    daily_pnl: float
    consecutive_losses: int
    kill_switch: bool
    paused: bool
    last_trigger_price: float
    last_trigger_at: datetime | None


class RuntimeStateService:
    def load(self, strategy_service: RuntimeStateStore, engine: StrategyEngine, risk: RiskController) -> None:
        config = strategy_service.get_config()
        state = strategy_service.get_runtime_state()

        engine.params = StrategyParams(
            symbol=config.symbol,
            market=config.market,
            buy_low=config.buy_low,
            sell_high=config.sell_high,
            short_selling=config.short_selling,
        )
        try:
            engine.state = EngineState(state.engine_state)
        except ValueError:
            logger.warning("invalid engine state %r in DB, defaulting to FLAT", state.engine_state)
            engine.state = EngineState.FLAT
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

    def snapshot(self, engine: StrategyEngine, risk: RiskController) -> RuntimeStateSnapshotValues:
        return RuntimeStateSnapshotValues(
            engine_state=engine.state.value,
            last_price=engine.last_price,
            daily_pnl=risk.daily_pnl,
            consecutive_losses=risk.consecutive_losses,
            kill_switch=risk.kill_switch,
            paused=risk.paused,
            last_trigger_price=engine.last_trigger_price,
            last_trigger_at=engine.last_trigger_at,
        )

    def persist_snapshot(self, strategy_service: RuntimeStateStore, snapshot: RuntimeStateSnapshotValues) -> None:
        strategy_service.update_runtime_state(
            engine_state=snapshot.engine_state,
            last_price=snapshot.last_price,
            daily_pnl=snapshot.daily_pnl,
            consecutive_losses=snapshot.consecutive_losses,
            kill_switch=snapshot.kill_switch,
            paused=snapshot.paused,
            last_trigger_price=snapshot.last_trigger_price,
            last_trigger_at=snapshot.last_trigger_at,
        )

    def persist(self, strategy_service: RuntimeStateStore, engine: StrategyEngine, risk: RiskController) -> None:
        self.persist_snapshot(strategy_service, self.snapshot(engine, risk))
