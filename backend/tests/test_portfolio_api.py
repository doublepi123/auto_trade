from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import database, main as main_module
from app.main import app
from app.models import Base


@pytest.fixture
def patched_app(monkeypatch):
    monkeypatch.setattr(main_module, "init_db", lambda: None)
    yield


def test_portfolio_config_name_mismatch_returns_400(patched_app) -> None:
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as client:
        resp = client.put(
            "/api/portfolio/config/wrong",
            json={"name": "demo", "symbols": ["AAPL.US"], "allocations": {"AAPL.US": 1.0}},
        )
    assert resp.status_code == 400


def test_portfolio_config_missing_fields_returns_422(patched_app) -> None:
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as client:
        resp = client.put(
            "/api/portfolio/config/demo",
            json={"name": "demo", "symbols": ["AAPL.US"]},
        )
    assert resp.status_code == 422


def test_portfolio_config_invalid_allocations_returns_422(patched_app) -> None:
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as client:
        resp = client.put(
            "/api/portfolio/config/demo",
            json={
                "name": "demo",
                "symbols": ["AAPL.US", "TSLA.US"],
                "allocations": {"AAPL.US": 0.6, "TSLA.US": 0.6},
            },
        )
    assert resp.status_code == 422


def test_portfolio_attribution_404_for_unknown(patched_app) -> None:
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as client:
        resp = client.get("/api/portfolio/attribution", params={"name": "missing"})
    assert resp.status_code == 404


def test_kill_switch_endpoints(patched_app) -> None:
    from app.platform import portfolio_runner as prm

    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    prm.reset_kill_switch_for_tests()
    with TestClient(app) as client:
        status0 = client.get("/api/portfolio/kill-switch").json()
        assert status0["armed"] is False
        armed = client.post("/api/portfolio/kill-switch").json()
        assert armed["armed"] is True
        assert client.get("/api/portfolio/kill-switch").json()["armed"] is True
        disarmed = client.post("/api/portfolio/kill-switch/disable").json()
        assert disarmed["armed"] is False
    prm.reset_kill_switch_for_tests()
