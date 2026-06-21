from __future__ import annotations

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

    monkeypatch.setattr(main_module, "get_runner", lambda: DummyRunner())
    monkeypatch.setattr(main_module, "init_db", lambda: None)

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
    assert hasattr(app.state, "platform_runner")


def test_platform_mode_disabled_leaves_runner_unset(patched_app, monkeypatch) -> None:
    monkeypatch.setattr(main_module.settings, "platform_mode", False)
    with TestClient(app) as client:
        response = client.get("/api/platform/strategies")
    assert response.status_code == 200
    assert app.state.platform_runner is None
