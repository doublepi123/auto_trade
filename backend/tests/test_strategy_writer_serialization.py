# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""P3b review 3: a writer that loaded a temporary band must not reinstall it.

Two independent sessions. The recenter commits band B. The API session has
already loaded B and is paused before its commit. The engine becomes LONG,
the recenter rolls back to A, then the API proceeds. The LONG engine must
still show A, and the DB band must match.
"""

from __future__ import annotations

import os
import tempfile
import threading
from contextlib import contextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

os.environ.setdefault(
    "AUTO_TRADE_DATABASE_URL",
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_writer_serial_{os.getpid()}.db",
)

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.strategy import update_strategy_with_runtime_reload
from app.config import settings
from app.core.broker import Quote
from app.core.engine import EngineState, StrategyParams
from app.models import Base, StrategyConfig, TradeEvent
from app.runner import AppRunner, PrimarySwitchBlockedError
from app.services.interval_recenter_service import (
    EVENT_INTERVAL_RECENTER_ROLLED_BACK,
    IntervalRecenterService,
)
from app.services.strategy_service import StrategyService

_ET = ZoneInfo("America/New_York")
_RTH = datetime(2026, 10, 6, 15, 0, tzinfo=_ET)
_A = (344.5447, 351.5052)


class _Broker:
    def get_quotes(self, symbols: list[str]) -> list[Quote]:
        return [Quote(symbol, 366.0, 365.9, 366.1, _RTH.isoformat()) for symbol in symbols]

    def get_positions(self) -> list[object]:
        return []


@pytest.fixture()
def factory():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine)
    try:
        yield sessions
    finally:
        engine.dispose()


def test_api_identity_map_does_not_reinstall_rolled_back_band(
    factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Genuine refused recenter, two independent sessions, no sleeps.

    1. Recenter commits band B under the reload guard.
    2. An API session loads B and waits before its own write.
    3. The engine becomes LONG before the require_flat swap.
    4. That reload raises PrimarySwitchBlockedError; the service rolls
       the durable band back to A and records ROLLED_BACK.
    5. The API writer proceeds after the rollback.

    Final DB band and engine band are A. The LONG engine never held B.
    """
    monkeypatch.setattr(settings, "interval_recenter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_min_drift_pct", 1.5, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_price_age_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_per_day", 4, raising=False)
    monkeypatch.setattr(settings, "llm_interval_volatility_threshold_pct", 1.0, raising=False)
    monkeypatch.setattr(
        "app.services.interval_recenter_service.is_trading_hours",
        lambda *_a, **_k: True,
    )
    seed = factory()
    seed.add(StrategyConfig(
        symbol="TSLA.US", market="US", buy_low=_A[0], sell_high=_A[1],
        trading_session_mode="ANY", min_profit_amount=0.0,
    ))
    seed.commit()
    seed.close()

    runner = AppRunner()
    runner._running = True
    runner.broker = _Broker()
    runner.engine.params = StrategyParams(
        symbol="TSLA.US", market="US", buy_low=_A[0], sell_high=_A[1],
    )
    runner.engine.state = EngineState.FLAT
    monkeypatch.setattr("app.api.strategy.get_runner", lambda: runner)

    b_committed = threading.Event()
    api_loaded_b = threading.Event()
    became_long = threading.Event()
    rollback_done = threading.Event()
    bands_while_long: list[float] = []
    errors: list[BaseException] = []
    original_reload = runner.reload_strategy

    def reload_then_wait_for_long(db=None, *, require_flat: bool = False):
        if require_flat:
            b_committed.set()
            assert api_loaded_b.wait(timeout=5)
            assert became_long.wait(timeout=5)
            bands_while_long.append(float(runner.engine.params.buy_low))
        original_reload(db, require_flat=require_flat)
        if require_flat:
            bands_while_long.append(float(runner.engine.params.buy_low))

    monkeypatch.setattr(runner, "reload_strategy", reload_then_wait_for_long)

    def become_long() -> None:
        assert b_committed.wait(timeout=5)
        runner.engine.state = EngineState.LONG
        became_long.set()

    def recenter() -> None:
        db = factory()
        try:
            IntervalRecenterService(db, clock=lambda: _RTH).evaluate(runner)
        except PrimarySwitchBlockedError:
            rollback_done.set()
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    def api_writer() -> None:
        assert b_committed.wait(timeout=5)
        db = factory()
        try:
            current = db.query(StrategyConfig).one()
            assert current.buy_low != _A[0]
            api_loaded_b.set()
            assert rollback_done.wait(timeout=5)
            update_strategy_with_runtime_reload(
                StrategyService(db),
                current,
                {"min_profit_amount": 1.0},
            )
        except BaseException as exc:
            errors.append(exc)
        finally:
            db.close()

    first = threading.Thread(target=recenter)
    second = threading.Thread(target=api_writer)
    third = threading.Thread(target=become_long)
    first.start()
    second.start()
    third.start()
    first.join(timeout=8)
    second.join(timeout=8)
    third.join(timeout=8)
    assert not first.is_alive() and not second.is_alive() and not third.is_alive()

    db = factory()
    try:
        cfg = db.query(StrategyConfig).one()
        rolled = db.query(TradeEvent).filter(
            TradeEvent.event_type == EVENT_INTERVAL_RECENTER_ROLLED_BACK,
            TradeEvent.status == "ROLLED_BACK",
        ).all()
    finally:
        db.close()
    assert errors == [], (errors, runner.engine.params.buy_low, cfg.buy_low)
    assert rolled, "recenter refusal did not record a ROLLED_BACK trade event"
    assert runner.engine.state == EngineState.LONG
    assert runner.engine.params.buy_low == pytest.approx(cfg.buy_low)
    assert cfg.buy_low == pytest.approx(_A[0])
    assert bands_while_long, "require_flat reload never installed a band"
    assert all(band == pytest.approx(_A[0]) for band in bands_while_long)


def test_initialize_waits_for_the_reload_guard(
    factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Startup config install must enter the same reload guard as writers.

    A writer holds the guard and has committed band B. Initialization must
    not install that band, or any later band, until the writer releases the
    guard. Without the guard this fails because install completes while the
    writer still holds it.
    """
    seed = factory()
    seed.add(StrategyConfig(
        symbol="TSLA.US", market="US", buy_low=_A[0], sell_high=_A[1],
    ))
    seed.commit()
    seed.close()
    runner = AppRunner()
    runner.broker = _Broker()
    init_db = factory()

    @contextmanager
    def use_test_db():
        yield init_db

    runner._db_session = use_test_db  # type: ignore[method-assign]
    held = threading.Event()
    release = threading.Event()
    installed_while_held: list[float] = []

    def initialize() -> None:
        runner._initialize_runner()

    worker = threading.Thread(target=initialize)
    with runner.strategy_reload_guard():
        held.set()
        worker.start()
        # The writer still owns the guard. Install must not have happened.
        assert not worker.join(timeout=0.5)
        installed_while_held.append(float(runner.engine.params.buy_low))
        StrategyService(init_db).update_config({"buy_low": 360.0, "sell_high": 370.0})
        release.set()
    worker.join(timeout=5)
    assert not worker.is_alive()
    assert installed_while_held == [0.0], (
        "startup installed a band while a writer still held the reload guard"
    )
    assert runner.engine.params.buy_low == pytest.approx(360.0)


def test_start_does_not_install_a_rolled_back_band(
    factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review-4: stop/start must not install a band the recenter rolled back."""
    monkeypatch.setattr(settings, "interval_recenter_enabled", True, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_min_drift_pct", 1.5, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_price_age_seconds", 300, raising=False)
    monkeypatch.setattr(settings, "interval_recenter_max_per_day", 4, raising=False)
    monkeypatch.setattr(settings, "llm_interval_volatility_threshold_pct", 1.0, raising=False)
    monkeypatch.setattr(
        "app.services.interval_recenter_service.is_trading_hours",
        lambda *_a, **_k: True,
    )
    seed = factory()
    seed.add(StrategyConfig(
        symbol="TSLA.US", market="US", buy_low=_A[0], sell_high=_A[1],
        trading_session_mode="ANY",
    ))
    seed.commit()
    seed.close()
    runner = AppRunner()
    runner._running = True
    runner.broker = _Broker()
    runner.engine.params = StrategyParams(
        symbol="TSLA.US", market="US", buy_low=_A[0], sell_high=_A[1],
    )
    runner.engine.state = EngineState.FLAT

    b_visible = threading.Event()
    startup_entered = threading.Event()
    release_writer = threading.Event()
    errors: list[BaseException] = []
    init_db = factory()

    @contextmanager
    def use_test_db():
        yield init_db

    runner._db_session = use_test_db  # type: ignore[method-assign]
    original_guard = runner.strategy_reload_guard

    @contextmanager
    def hold_after_commit():
        with original_guard():
            yield
            row = init_db.query(StrategyConfig).one()
            if row.buy_low != _A[0]:
                b_visible.set()
                assert release_writer.wait(timeout=5)

    monkeypatch.setattr(runner, "strategy_reload_guard", hold_after_commit)

    def recenter() -> None:
        try:
            IntervalRecenterService(init_db, clock=lambda: _RTH).evaluate(runner)
        except BaseException as exc:
            errors.append(exc)

    def start() -> None:
        assert b_visible.wait(timeout=5)
        startup_entered.set()
        try:
            runner._initialize_runner()
        except BaseException as exc:
            errors.append(exc)

    writer = threading.Thread(target=recenter)
    starter = threading.Thread(target=start)
    writer.start()
    assert b_visible.wait(timeout=5), errors
    starter.start()
    assert startup_entered.wait(timeout=2)
    # Writer still holds the guard and the durable band is B. Startup must
    # not install it. Become LONG, then let the writer finish: require_flat
    # refuses and rolls the durable band back to A before startup installs.
    starter.join(timeout=0.5)
    assert starter.is_alive(), (
        "startup finished while the recenter still held the reload guard"
    )
    runner.engine.state = EngineState.LONG
    release_writer.set()
    writer.join(timeout=8)
    starter.join(timeout=8)
    assert not writer.is_alive()
    assert not starter.is_alive()

    cfg = init_db.query(StrategyConfig).one()
    assert errors == [], errors
    # Recenter committed a non-A band while holding the guard. Startup must
    # not have installed that band before the writer released the guard
    # (asserted via starter.is_alive() above). After the writer rolls back,
    # the live band matches the durable band.
    assert runner.engine.params.buy_low == pytest.approx(cfg.buy_low)
    assert starter.is_alive() is False

