from __future__ import annotations

import json

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app import database, main as main_module
from app.main import app
from app.models import Base, StrategyConfig, StrategyParamVersion


def _patched(monkeypatch):
    monkeypatch.setattr(main_module, "init_db", lambda: None)


def _seed_strategy(db):
    cfg = db.query(StrategyConfig).first()
    if cfg is None:
        cfg = StrategyConfig(symbol="AAPL.US", market="US", buy_low=100.0, sell_high=110.0)
        db.add(cfg)
        db.commit()
    return cfg


def test_put_strategy_records_version(monkeypatch):
    _patched(monkeypatch)
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with Session(database.engine) as db:
        _seed_strategy(db)
    with TestClient(app) as client:
        resp = client.put("/api/strategy", json={"buy_low": 105.0, "sell_high": 115.0})
    assert resp.status_code == 200
    with Session(database.engine) as db:
        versions = db.query(StrategyParamVersion).all()
        assert len(versions) >= 1
        params = json.loads(versions[0].params_json)
        assert params["buy_low"] in (105.0, "105.0")


def test_list_and_rollback_version(monkeypatch):
    _patched(monkeypatch)
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with Session(database.engine) as db:
        _seed_strategy(db)
    with TestClient(app) as client:
        client.put("/api/strategy", json={"buy_low": 105.0, "sell_high": 115.0})
        listed = client.get("/api/strategy/versions").json()
        assert len(listed) >= 1
        version_id = listed[0]["id"]
        # change again so rollback is observable
        client.put("/api/strategy", json={"buy_low": 90.0, "sell_high": 120.0})
        rb = client.post(f"/api/strategy/versions/{version_id}/rollback")
    assert rb.status_code == 200
    assert rb.json()["rolled_back_to"] == version_id
    with Session(database.engine) as db:
        cfg = db.query(StrategyConfig).first()
        assert cfg is not None
        assert cfg.buy_low == 105.0


def test_rollback_unknown_version_404(monkeypatch):
    _patched(monkeypatch)
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with TestClient(app) as client:
        resp = client.post("/api/strategy/versions/9999/rollback")
    assert resp.status_code == 404


def test_rollback_revalidates_legacy_unsafe_version(monkeypatch):
    _patched(monkeypatch)
    Base.metadata.drop_all(bind=database.engine)
    Base.metadata.create_all(bind=database.engine)
    with Session(database.engine) as db:
        cfg = _seed_strategy(db)
        row = StrategyParamVersion(
            params_json=json.dumps(
                {
                    "symbol": cfg.symbol,
                    "market": cfg.market,
                    "buy_low": cfg.buy_low,
                    "sell_high": cfg.sell_high,
                    "short_selling": True,
                }
            )
        )
        db.add(row)
        db.commit()
        version_id = row.id

    with TestClient(app) as client:
        response = client.post(f"/api/strategy/versions/{version_id}/rollback")

    assert response.status_code == 422
    with Session(database.engine) as db:
        cfg = db.query(StrategyConfig).first()
        assert cfg is not None
        assert cfg.short_selling is False
