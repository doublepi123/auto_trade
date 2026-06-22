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
