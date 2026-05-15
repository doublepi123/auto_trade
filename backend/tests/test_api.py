import os

os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_api.db"


from fastapi.testclient import TestClient

from app.database import engine as db_engine, SessionLocal
from app.models import Base, CredentialConfig, StrategyConfig
from app.main import app


Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


def _clean_strategy() -> None:
    db = SessionLocal()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()


def _clean_credentials() -> None:
    db = SessionLocal()
    db.query(CredentialConfig).delete()
    db.commit()
    db.close()


class TestAPI:
    def test_health(self) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_get_strategy_default(self) -> None:
        _clean_strategy()
        resp = client.get("/api/strategy")
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == ""
        assert "sct_key" not in data

    def test_get_credentials_default(self) -> None:
        _clean_credentials()
        resp = client.get("/api/credentials")
        assert resp.status_code == 200
        data = resp.json()
        assert data["longbridge_app_key"] == ""

    def test_update_strategy_valid(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
            "short_selling": False,
            "max_daily_loss": 5000.0,
            "max_consecutive_losses": 3,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["buy_low"] == 100.0
        assert data["sell_high"] == 200.0
        assert "sct_key" not in data

    def test_update_strategy_invalid_market(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "CN",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_sell_lt_buy(self) -> None:
        resp = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 200.0,
            "sell_high": 100.0,
        })
        assert resp.status_code == 422

    def test_update_strategy_rejects_partial_threshold_inversion(self) -> None:
        _clean_strategy()
        initial = client.put("/api/strategy", json={
            "symbol": "AAPL.US",
            "market": "US",
            "buy_low": 100.0,
            "sell_high": 200.0,
        })
        assert initial.status_code == 200

        resp = client.put("/api/strategy", json={"buy_low": 250.0})

        assert resp.status_code == 422

    def test_get_status(self) -> None:
        resp = client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert "engine_state" in data

    def test_pause_trading(self) -> None:
        resp = client.post("/api/control/pause", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading paused"

    def test_resume_trading(self) -> None:
        resp = client.post("/api/control/resume")
        assert resp.status_code == 200
        assert resp.json()["message"] == "trading resumed"

    def test_kill_switch(self) -> None:
        resp = client.post("/api/control/kill-switch", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "kill switch activated"

    def test_start_runner(self) -> None:
        resp = client.post("/api/control/start")
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner started"

    def test_stop_runner(self) -> None:
        resp = client.post("/api/control/stop", json={"reason": "testing"})
        assert resp.status_code == 200
        assert resp.json()["message"] == "runner stopped"

    def test_get_orders_empty(self) -> None:
        resp = client.get("/api/orders")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_lifespan_initializes_db(self) -> None:
        resp = client.get("/api/health")
        assert resp.status_code == 200

    def test_get_account(self) -> None:
        resp = client.get("/api/account")
        assert resp.status_code == 200
        data = resp.json()
        assert "total_assets" in data
        assert "cash_balances" in data
        assert "positions" in data

    def test_app_title(self) -> None:
        assert app.title == "Auto Trade"
