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
