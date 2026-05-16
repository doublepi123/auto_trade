from __future__ import annotations

import logging
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.engine import EngineState, StrategyEngine
from app.core.risk import RiskController
from app.services.runtime_state_service import RuntimeStateService


class FakeStrategyService:
    def __init__(self) -> None:
        self.persisted: dict[str, object] | None = None

    def get_config(self) -> object:
        return SimpleNamespace(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=200.0, short_selling=True, max_daily_loss=1234.0, max_consecutive_losses=2)

    def get_runtime_state(self) -> object:
        return SimpleNamespace(engine_state="short", last_price=150.0, last_trigger_price=201.0, last_trigger_at=None, daily_pnl=-50.0, consecutive_losses=1, kill_switch=True, paused=True)

    def update_runtime_state(self, **kwargs: object) -> object:
        self.persisted = kwargs
        return SimpleNamespace(**kwargs)


def test_load_applies_config_and_runtime_state() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    engine = StrategyEngine()
    risk = RiskController()

    service.load(strategy_service, engine, risk)

    assert engine.params.symbol == "AAPL.US"
    assert engine.params.market == "US"
    assert engine.params.buy_low == 100.0
    assert engine.params.sell_high == 200.0
    assert engine.params.short_selling is True
    assert engine.state == EngineState.SHORT
    assert engine.last_price == 150.0
    assert engine.last_trigger_price == 201.0
    assert engine.last_trigger_at is None
    assert risk.config.max_daily_loss == 1234.0
    assert risk.config.max_consecutive_losses == 2
    assert risk.daily_pnl == -50.0
    assert risk.consecutive_losses == 1
    assert risk.kill_switch is True
    assert risk.paused is True


def test_load_invalid_engine_state_defaults_to_flat(caplog) -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    strategy_service.get_runtime_state = lambda: SimpleNamespace(engine_state="broken", last_price=1.0, last_trigger_price=0.0, last_trigger_at=None, daily_pnl=0.0, consecutive_losses=0, kill_switch=False, paused=False)
    engine = StrategyEngine()

    with caplog.at_level(logging.WARNING):
        service.load(strategy_service, engine, RiskController())

    assert engine.state == EngineState.FLAT
    assert "invalid engine state" in caplog.text


def test_persist_writes_current_snapshot() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    engine = StrategyEngine()
    risk = RiskController()
    last_trigger_at = datetime(2026, 5, 16, tzinfo=timezone.utc)
    engine.state = EngineState.LONG
    engine.last_price = 188.0
    engine.last_trigger_price = 187.5
    engine.last_trigger_at = last_trigger_at
    risk.daily_pnl = 42.0
    risk.consecutive_losses = 2
    risk.kill_switch = True
    risk.paused = True

    service.persist(strategy_service, engine, risk)

    assert strategy_service.persisted == {
        "engine_state": "long",
        "last_price": 188.0,
        "daily_pnl": 42.0,
        "consecutive_losses": 2,
        "kill_switch": True,
        "paused": True,
        "last_trigger_price": 187.5,
        "last_trigger_at": last_trigger_at,
    }


def test_snapshot_and_persist_snapshot_write_copied_values() -> None:
    service = RuntimeStateService()
    strategy_service = FakeStrategyService()
    engine = StrategyEngine()
    risk = RiskController()
    last_trigger_at = datetime(2026, 5, 16, tzinfo=timezone.utc)
    engine.state = EngineState.LONG
    engine.last_price = 188.0
    engine.last_trigger_price = 187.5
    engine.last_trigger_at = last_trigger_at
    risk.daily_pnl = 42.0
    risk.consecutive_losses = 2
    risk.kill_switch = True
    risk.paused = True

    snapshot = service.snapshot(engine, risk)
    engine.state = EngineState.FLAT
    engine.last_price = 1.0
    risk.daily_pnl = 0.0

    service.persist_snapshot(strategy_service, snapshot)

    assert strategy_service.persisted == {
        "engine_state": "long",
        "last_price": 188.0,
        "daily_pnl": 42.0,
        "consecutive_losses": 2,
        "kill_switch": True,
        "paused": True,
        "last_trigger_price": 187.5,
        "last_trigger_at": last_trigger_at,
    }
