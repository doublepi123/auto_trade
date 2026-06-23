from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app


@pytest.fixture
def patched_app(monkeypatch):
    class DummyRunner:
        def start(self, *, loop=None):
            return True

        def stop(self):
            pass

        def diagnostics(self):
            return {}

    class FakeStrategyService:
        def __init__(self, db=None):
            pass

        def get_config(self):
            return SimpleNamespace(
                symbol="AAPL.US",
                buy_low=145.0,
                sell_high=155.0,
                quantity=10,
            )

    monkeypatch.setattr(main_module, "get_runner", lambda: DummyRunner())
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module, "StrategyService", FakeStrategyService)

    original_platform_mode = main_module.settings.platform_mode
    original_platform_runner = getattr(app.state, "platform_runner", None)

    yield

    main_module.settings.platform_mode = original_platform_mode
    app.state.platform_runner = original_platform_runner


def test_platform_strategies_endpoint_lists_interval_strategy(patched_app, monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "platform_mode", True)
    with TestClient(app) as client:
        response = client.get("/api/platform/strategies")
    assert response.status_code == 200
    data = response.json()
    assert any(s["name"] == "interval" for s in data)

    platform_runner = app.state.platform_runner
    assert platform_runner is not None
    assert platform_runner.symbol == "AAPL.US"
    assert platform_runner.mode == "live"
    assert platform_runner.strategy.name == "interval"
    assert platform_runner.strategy.params == {
        "buy_low": Decimal("145"),
        "sell_high": Decimal("155"),
        "quantity": 10,
    }


def test_platform_mode_disabled_leaves_runner_unset(patched_app, monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "platform_mode", False)
    with TestClient(app) as client:
        response = client.get("/api/platform/strategies")
    assert response.status_code == 200
    assert app.state.platform_runner is None


def test_platform_backtest_endpoint_runs(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/backtest",
            json={
                "strategy": "interval",
                "params": {"buy_low": 145, "sell_high": 155, "quantity": 10},
                "symbols": ["AAPL.US"],
                "initial_cash": 10000,
                "bars": [
                    {"timestamp": "2026-06-22T10:00:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 144, "volume": 1000},
                    {"timestamp": "2026-06-22T10:01:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 156, "volume": 1000},
                ],
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["fills"]) == 2
    assert data["stats"]["num_bars"] == 2
    assert data["stats"]["num_fills"] == 2
    assert data["final_positions"]["AAPL.US"] == 0
    assert len(data["equity_curve"]) == 2


def test_platform_backtest_missing_fields_returns_422(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/platform/backtest", json={"strategy": "interval"})
    assert resp.status_code == 422


def test_platform_backtest_unknown_strategy_returns_404(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/backtest",
            json={
                "strategy": "nope",
                "params": {},
                "symbols": ["AAPL.US"],
                "bars": [
                    {"timestamp": "2026-06-22T10:00:00+00:00", "symbol": "AAPL.US", "open": 1, "high": 2, "low": 0, "close": 1, "volume": 1},
                ],
            },
        )
    assert resp.status_code == 404


def test_platform_backtest_empty_symbols_or_bars_returns_422(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/backtest",
            json={"strategy": "interval", "params": {}, "symbols": [], "bars": []},
        )
    assert resp.status_code == 422


def test_platform_snapshot_404_when_runner_disabled(patched_app, monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "platform_mode", False)
    with TestClient(app) as client:
        resp = client.get("/api/platform/snapshot")
    assert resp.status_code == 404


def test_platform_snapshot_reports_positions_and_open_orders(patched_app, monkeypatch) -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from app.platform.bus import EventBus
    from app.platform.events import BarEvent, EventSource
    from app.platform.runner import PlatformRunner
    from app.strategies.interval_strategy import IntervalStrategy

    monkeypatch.setattr(main_module.settings, "platform_mode", True)
    strategy = IntervalStrategy(
        params={"buy_low": Decimal("145"), "sell_high": Decimal("155"), "quantity": 10}
    )
    runner = PlatformRunner(symbols=["AAPL.US"], strategy=strategy, mode="paper", bus=EventBus())
    # drive a bar to create a BUY fill -> position 10
    runner.on_bar(
        BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    with TestClient(app) as client:
        # Override the runner set by lifespan with our instrumented paper runner.
        app.state.platform_runner = runner
        resp = client.get("/api/platform/snapshot")
    assert resp.status_code == 200
    data = resp.json()
    assert data["mode"] == "paper"
    assert data["symbols"] == ["AAPL.US"]
    assert any(p["symbol"] == "AAPL.US" and p["quantity"] == 10 for p in data["positions"])


def test_platform_events_endpoint_returns_stored_events(patched_app) -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from app import database
    from app.models import Base
    from app.platform.events import BarEvent, EventSource
    from app.platform.store import EventStore

    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    store = EventStore()
    store.clear()
    store.append(
        BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("151"),
            low=Decimal("149"),
            close=Decimal("150.5"),
            volume=100,
        )
    )
    with TestClient(app) as client:
        resp = client.get("/api/platform/events", params={"symbol": "AAPL.US", "limit": 10})
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] >= 1
    assert data["events"][0]["event_type"] == "bar"


def test_platform_events_limit_validation(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/platform/events", params={"limit": 0})
    assert resp.status_code == 422


def test_platform_replay_reconstructs_positions(patched_app) -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from app import database
    from app.models import Base
    from app.platform.events import BarEvent, EventSource
    from app.platform.store import EventStore

    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    store = EventStore()
    store.clear()
    # persist two bars that trigger interval BUY then SELL
    store.append(
        BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("144"),
            volume=1000,
        )
    )
    store.append(
        BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            source=EventSource.MARKET,
            symbol="AAPL.US",
            open=Decimal("150"),
            high=Decimal("160"),
            low=Decimal("140"),
            close=Decimal("156"),
            volume=1000,
        )
    )
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/replay",
            json={
                "strategy": "interval",
                "params": {"buy_low": 145, "sell_high": 155, "quantity": 10},
                "symbols": ["AAPL.US"],
                "symbol": "AAPL.US",
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["bars_replayed"] == 2
    assert data["reconstructed_positions"] == []  # round-trip: flat


def test_platform_analyze_endpoint(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/analyze",
            json={"equity_curve": [{"nav": 10000}, {"nav": 10500}, {"nav": 10200}], "periods_per_year": 252},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "sharpe" in data and "max_drawdown" in data and "total_return" in data


def test_platform_analyze_missing_equity_422(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/platform/analyze", json={})
    assert resp.status_code == 422


def test_platform_backtest_includes_analytics(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/backtest",
            json={
                "strategy": "interval", "params": {"buy_low": 145, "sell_high": 155, "quantity": 10},
                "symbols": ["AAPL.US"],
                "initial_cash": 10000,
                "bars": [
                    {"timestamp": "2026-06-22T10:00:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 144, "volume": 1000},
                    {"timestamp": "2026-06-22T10:01:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 156, "volume": 1000},
                ],
            },
        )
    assert resp.status_code == 200
    assert "analytics" in resp.json()
    assert "sharpe" in resp.json()["analytics"]


def test_platform_bars_endpoint(patched_app) -> None:
    from datetime import datetime, timezone
    from decimal import Decimal

    from app import database
    from app.models import Base
    from app.platform.events import BarEvent, EventSource
    from app.platform.store import EventStore

    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    store = EventStore()
    store.clear()
    for m in range(6):
        store.append(
            BarEvent(
                timestamp=datetime(2026, 6, 23, 10, m, tzinfo=timezone.utc),
                source=EventSource.MARKET,
                symbol="A",
                open=Decimal("10"),
                high=Decimal("11"),
                low=Decimal("9"),
                close=Decimal("10"),
                volume=100,
            )
        )
    with TestClient(app) as client:
        resp = client.get(
            "/api/platform/bars",
            params={"symbol": "A", "resolution_minutes": 5, "limit": 100},
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["count"] == 2
    assert data["bars"][0]["high"] == "11"


def test_platform_bars_symbol_required(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.get("/api/platform/bars", params={"symbol": ""})
    assert resp.status_code == 422


def test_platform_optimize_endpoint(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/optimize",
            json={
                "strategy": "interval",
                "param_grid": {"buy_low": [145, 146], "sell_high": [154, 155], "quantity": [10]},
                "symbols": ["AAPL.US"],
                "bars": [
                    {"timestamp": "2026-06-23T10:00:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 144, "volume": 1000},
                    {"timestamp": "2026-06-23T10:01:00+00:00", "symbol": "AAPL.US", "open": 150, "high": 160, "low": 140, "close": 156, "volume": 1000},
                ],
                "metric": "sharpe",
                "top_k": 3,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_combos"] == 4
    assert len(data["ranked"]) <= 3


def test_platform_optimize_missing_fields_422(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post("/api/platform/optimize", json={"strategy": "interval"})
    assert resp.status_code == 422


def test_platform_analyze_with_benchmark(patched_app) -> None:
    with TestClient(app) as client:
        resp = client.post(
            "/api/platform/analyze",
            json={
                "equity_curve": [{"nav": 10000 * (1.005 ** i)} for i in range(20)],
                "benchmark_equity": [{"nav": 10000 * (1.002 ** i)} for i in range(20)],
                "periods_per_year": 252,
            },
        )
    assert resp.status_code == 200
    data = resp.json()
    assert "benchmark" in data
    assert "beta" in data["benchmark"]
    assert "excess_return" in data["benchmark"]
