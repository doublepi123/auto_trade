# pyright: reportArgumentType=false, reportAttributeAccessIssue=false, reportReturnType=false, reportInvalidTypeForm=false
"""P3b review 1: recenter must not run outside an executable session,
and the same-symbol short-circuit must not skip the flatness checks.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_recenter_session_{os.getpid()}.db",
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.core.broker import Position, Quote
from app.core.engine import EngineState, StrategyParams
from app.models import Base, StrategyConfig, TradeEvent
from app.runner import AppRunner, PrimarySwitchBlockedError
from app.services.interval_recenter_service import (
    EVENT_INTERVAL_RECENTERED,
    EVENT_INTERVAL_RECENTER_BLOCKED,
    EVENT_INTERVAL_RECENTER_ROLLED_BACK,
    OUTCOME_BLOCKED,
    OUTCOME_RECENTERED,
    IntervalRecenterService,
)

OUTCOME_OUTSIDE_SESSION = "OUTSIDE_SESSION"

_ET = ZoneInfo("America/New_York")
_PRE = datetime(2026, 10, 6, 5, 0, tzinfo=_ET)


class _Quote:
    def __init__(self, symbol: str, last_price: float, at: datetime) -> None:
        self.symbol = symbol
        self.last_price = last_price
        self.timestamp = at


class _Broker:
    def __init__(self, price: float, at: datetime) -> None:
        self.price = price
        self.at = at
        self.positions: list[Position] = []

    def get_quotes(self, symbols: list[str]) -> list[_Quote]:
        return [_Quote(symbol, self.price, self.at) for symbol in symbols]

    def get_positions(self) -> list[Position]:
        return list(self.positions)


def _seed(db: Session, *, mode: str) -> None:
    db.query(StrategyConfig).delete()
    db.query(TradeEvent).delete()
    db.add(StrategyConfig(
        symbol="TSLA.US",
        market="US",
        buy_low=344.5447,
        sell_high=351.5052,
        trading_session_mode=mode,
    ))
    db.commit()


def _events(db: Session) -> list[str]:
    return [row.event_type for row in db.query(TradeEvent).all()]


@pytest.fixture()
def db() -> Session:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(bind=engine)
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _enable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "interval_recenter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_min_drift_pct", 1.5, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_price_age_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_per_day", 4, raising=False)
    monkeypatch.setattr(settings, "llm_interval_volatility_threshold_pct", 1.0, raising=False)
    monkeypatch.setattr(settings, "extended_hours_trading_enabled", False, raising=False)
    monkeypatch.setattr(settings, "paper_account_confirmed", False, raising=False)


def _runner(broker: _Broker) -> AppRunner:
    runner = AppRunner()
    runner._running = True
    runner.broker = broker
    runner.engine.params = StrategyParams(
        symbol="TSLA.US",
        market="US",
        buy_low=344.5447,
        sell_high=351.5052,
    )
    runner.engine.state = EngineState.FLAT
    return runner


def _evaluate(db: Session, runner: AppRunner) -> object:
    return IntervalRecenterService(db, clock=lambda: _PRE).evaluate(runner)


def test_paper_or_rth_only_at_0500_does_not_recenter(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "paper_account_confirmed", True, raising=False)
    _seed(db, mode="RTH_ONLY")
    runner = _runner(_Broker(366.0, _PRE))
    before = (runner.engine.params.buy_low, runner.engine.params.sell_high)

    result = _evaluate(db, runner)

    assert result.outcome == OUTCOME_OUTSIDE_SESSION
    assert _events(db) == []
    assert (runner.engine.params.buy_low, runner.engine.params.sell_high) == before
    cfg = db.query(StrategyConfig).one()
    assert cfg.buy_low == 344.5447
    assert cfg.sell_high == 351.5052


def test_any_extended_effective_flat_at_0500_recenters(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "extended_hours_trading_enabled", True, raising=False)
    _seed(db, mode="ANY")
    runner = _runner(_Broker(366.0, _PRE))
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trading_session_mode = "ANY"

    result = _evaluate(db, runner)

    assert result.outcome == OUTCOME_RECENTERED
    assert EVENT_INTERVAL_RECENTERED in _events(db)
    assert runner.engine.params.buy_low != 344.5447


def test_rth_long_same_symbol_is_blocked(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.market_calendar.is_trading_hours",
        lambda *_args, **_kwargs: True,
    )
    _seed(db, mode="ANY")
    now = datetime(2026, 10, 6, 15, 0, tzinfo=_ET)
    runner = _runner(_Broker(366.0, now))
    runner.engine.state = EngineState.LONG

    result = IntervalRecenterService(db, clock=lambda: now).evaluate(runner)

    assert result.outcome == OUTCOME_BLOCKED
    assert EVENT_INTERVAL_RECENTER_BLOCKED in _events(db)
    cfg = db.query(StrategyConfig).one()
    assert cfg.buy_low == 344.5447


def test_race_to_long_between_check_and_reload_rolls_back(
    db: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "app.core.market_calendar.is_trading_hours",
        lambda *_args, **_kwargs: True,
    )
    _seed(db, mode="ANY")
    now = datetime(2026, 10, 6, 15, 0, tzinfo=_ET)
    runner = _runner(_Broker(366.0, now))
    original = runner.reload_strategy

    def flip_then_reload(session: object = None, *, require_flat: bool = False) -> None:
        runner.engine.state = EngineState.LONG
        assert require_flat is True
        original(session, require_flat=require_flat)

    runner.reload_strategy = flip_then_reload  # type: ignore[method-assign]

    with pytest.raises(PrimarySwitchBlockedError, match="engine state"):
        IntervalRecenterService(db, clock=lambda: now).evaluate(runner)
    assert runner.engine.state == EngineState.LONG

    cfg = db.query(StrategyConfig).one()
    assert cfg.buy_low == pytest.approx(344.5447)
    assert EVENT_INTERVAL_RECENTER_ROLLED_BACK in _events(db)
    assert EVENT_INTERVAL_RECENTERED not in _events(db)
