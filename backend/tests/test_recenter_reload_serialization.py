# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""P3b review 2: a shared one-shot flag is not a mutation boundary.

Two threads must not interleave a plain reload's late swap with a
recenter rollback. The engine band must equal the DB band, and a LONG
engine must not receive the recentered band.
"""

from __future__ import annotations

import os
import tempfile
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_recenter_serial_{os.getpid()}.db",
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.core.broker import Quote
from app.core.engine import EngineState, StrategyParams
from app.models import Base, StrategyConfig, TradeEvent
from app.runner import AppRunner
from app.services.interval_recenter_service import (
    EVENT_INTERVAL_RECENTER_ROLLED_BACK,
    IntervalRecenterService,
)
from app.services.strategy_service import StrategyService

_ET = ZoneInfo("America/New_York")
_RTH = datetime(2026, 10, 6, 15, 0, tzinfo=_ET)


class _Broker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [
            Quote(symbol, 366.0, 365.9, 366.1, _RTH.isoformat())
            for symbol in symbols
        ]

    def get_positions(self) -> list[object]:
        return []


@pytest.fixture()
def sessions():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _seed(factory) -> None:
    db = factory()
    db.add(StrategyConfig(
        symbol="TSLA.US",
        market="US",
        buy_low=344.5447,
        sell_high=351.5052,
        trading_session_mode="ANY",
    ))
    db.commit()
    db.close()


def test_plain_reload_and_refused_recenter_leave_one_band(
    sessions,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "interval_recenter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_min_drift_pct", 1.5, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_price_age_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_per_day", 4, raising=False)
    monkeypatch.setattr(settings, "llm_interval_volatility_threshold_pct", 1.0, raising=False)
    monkeypatch.setattr(
        "app.core.market_calendar.is_trading_hours",
        lambda market, instant=None: True,
    )
    _seed(sessions)
    runner = AppRunner()
    runner._running = True
    runner.broker = _Broker()
    runner.engine.params = StrategyParams(
        symbol="TSLA.US", market="US", buy_low=344.5447, sell_high=351.5052,
    )
    runner.engine.state = EngineState.FLAT

    read_entered = threading.Event()
    release_read = threading.Event()
    original = StrategyService.get_config

    def paused_get_config(self):
        config = original(self)
        if not read_entered.is_set():
            read_entered.set()
            release_read.wait(timeout=5)
        return config

    monkeypatch.setattr(StrategyService, "get_config", paused_get_config)

    errors: list[BaseException] = []

    def plain_reload() -> None:
        try:
            db = sessions()
            try:
                runner.reload_strategy(db)
            finally:
                db.close()
        except BaseException as exc:
            errors.append(exc)

    def recenter_then_long() -> None:
        assert read_entered.wait(timeout=5)
        original_reload = runner.reload_strategy

        def reload_then_long(db=None, *, require_flat: bool = False):
            if require_flat:
                runner.engine.state = EngineState.LONG
            return original_reload(db, require_flat=require_flat)

        runner.reload_strategy = reload_then_long  # type: ignore[method-assign]
        db = sessions()
        try:
            IntervalRecenterService(db, clock=lambda: _RTH).evaluate(runner)
        except BaseException as exc:
            if not isinstance(exc, Exception) or "engine state" not in str(exc):
                errors.append(exc)
        finally:
            db.close()
            release_read.set()

    first = threading.Thread(target=plain_reload)
    second = threading.Thread(target=recenter_then_long)
    first.start()
    second.start()
    first.join(timeout=8)
    second.join(timeout=8)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    db = sessions()
    try:
        cfg = db.query(StrategyConfig).one()
        events = [row.event_type for row in db.query(TradeEvent).all()]
    finally:
        db.close()
    assert (runner.engine.params.buy_low, runner.engine.params.sell_high) == (
        cfg.buy_low,
        cfg.sell_high,
    )
    assert cfg.buy_low == pytest.approx(344.5447)
    assert EVENT_INTERVAL_RECENTER_ROLLED_BACK in events


def test_require_flat_reload_while_long_raises(sessions) -> None:
    _seed(sessions)
    runner = AppRunner()
    runner._running = True
    runner.broker = _Broker()
    runner.engine.state = EngineState.LONG
    runner.engine.params = StrategyParams(
        symbol="TSLA.US", market="US", buy_low=344.5447, sell_high=351.5052,
    )
    db = sessions()
    try:
        StrategyService(db).update_config({"buy_low": 360.0, "sell_high": 370.0})
        with pytest.raises(Exception):
            runner.reload_strategy(db, require_flat=True)
    finally:
        db.close()
    assert runner.engine.params.buy_low == pytest.approx(344.5447)
