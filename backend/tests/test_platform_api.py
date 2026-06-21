from fastapi.testclient import TestClient

from app import main as main_module
from app.main import app


def test_platform_strategies_endpoint_lists_interval_strategy(monkeypatch) -> None:
    class DummyRunner:
        def start(self, *, loop=None):
            return True
        def stop(self):
            pass
        def diagnostics(self):
            return {}

    monkeypatch.setattr(main_module, "get_runner", lambda: DummyRunner())
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    monkeypatch.setattr(main_module.settings, "platform_mode", True)
    client = TestClient(app)
    response = client.get("/api/platform/strategies")
    assert response.status_code == 200
    data = response.json()
    assert any(s["name"] == "interval" for s in data)
