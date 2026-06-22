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
