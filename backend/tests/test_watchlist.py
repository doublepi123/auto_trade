import os
os.environ["AUTO_TRADE_DATABASE_URL"] = "sqlite:///data/test_watchlist.db"

from app.database import engine as db_engine, SessionLocal
from app.models import Base, StrategyConfig, WatchlistItem
from app.main import app
from app.services.watchlist_service import WatchlistService
from fastapi.testclient import TestClient
import pytest

Base.metadata.create_all(bind=db_engine)

client = TestClient(app)


@pytest.fixture
def clean_db():
    db = SessionLocal()
    db.query(WatchlistItem).delete()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()
    yield
    db = SessionLocal()
    db.query(WatchlistItem).delete()
    db.query(StrategyConfig).delete()
    db.commit()
    db.close()


class TestWatchlistApi:
    def test_get_empty(self, clean_db):
        resp = client.get("/api/watchlist")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_add_item(self, clean_db):
        resp = client.post("/api/watchlist", json={"symbol": "AAPL.US", "market": "US", "alias": "Apple"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["market"] == "US"
        assert data["alias"] == "Apple"
        assert data["is_active"] is False

    def test_add_duplicate(self, clean_db):
        client.post("/api/watchlist", json={"symbol": "AAPL.US", "market": "US"})
        resp = client.post("/api/watchlist", json={"symbol": "AAPL.US", "market": "US"})
        assert resp.status_code == 409

    def test_remove_item(self, clean_db):
        add = client.post("/api/watchlist", json={"symbol": "TSLA.US", "market": "US"})
        item_id = add.json()["id"]
        resp = client.delete(f"/api/watchlist/{item_id}")
        assert resp.status_code == 200
        assert resp.json()["message"] == "removed"

    def test_remove_not_found(self, clean_db):
        resp = client.delete("/api/watchlist/9999")
        assert resp.status_code == 404

    def test_activate_sets_single_trading(self, clean_db):
        # Create strategy config first
        db = SessionLocal()
        strategy = StrategyConfig(symbol="", market="US")
        db.add(strategy)
        db.commit()
        db.close()

        # Add two items
        r1 = client.post("/api/watchlist", json={"symbol": "AAPL.US", "market": "US"})
        id1 = r1.json()["id"]
        r2 = client.post("/api/watchlist", json={"symbol": "TSLA.US", "market": "US"})
        id2 = r2.json()["id"]

        # Activate first
        resp = client.post(f"/api/watchlist/{id1}/set-trading")
        assert resp.status_code == 200
        assert resp.json()["is_active"] is True

        # Check only one is active in DB
        db = SessionLocal()
        active_count = db.query(WatchlistItem).filter(WatchlistItem.is_active == True).count()
        db.close()
        assert active_count == 1

        # Activate second
        resp2 = client.post(f"/api/watchlist/{id2}/set-trading")
        assert resp2.status_code == 200
        assert resp2.json()["is_active"] is True

        # First should be inactive now
        db = SessionLocal()
        item1 = db.query(WatchlistItem).filter(WatchlistItem.id == id1).first()
        assert item1 is not None
        assert item1.is_active is False
        item2 = db.query(WatchlistItem).filter(WatchlistItem.id == id2).first()
        assert item2 is not None
        assert item2.is_active is True
        db.close()

    def test_activate_syncs_strategy_config(self, clean_db):
        db = SessionLocal()
        strategy = StrategyConfig(symbol="OLD.US", market="US")
        db.add(strategy)
        db.commit()
        db.close()

        r = client.post("/api/watchlist", json={"symbol": "NVDA.US", "market": "US"})
        item_id = r.json()["id"]

        resp = client.post(f"/api/watchlist/{item_id}/set-trading")
        assert resp.status_code == 200

        db = SessionLocal()
        strategy = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
        assert strategy is not None
        assert strategy.symbol == "NVDA.US"
        assert strategy.market == "US"
        db.close()

    def test_symbol_validation(self, clean_db):
        resp = client.post("/api/watchlist", json={"symbol": "INVALID", "market": "US"})
        assert resp.status_code == 422

    def test_market_validation(self, clean_db):
        resp = client.post("/api/watchlist", json={"symbol": "AAPL.US", "market": "XX"})
        assert resp.status_code == 422


    def test_get_watchlist_snapshots_empty(self, clean_db):
        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_watchlist_snapshots_marks_current_trading_symbol(self, clean_db, monkeypatch):
        from datetime import datetime, timezone
        from decimal import Decimal
        from types import SimpleNamespace

        from app.api import watchlist as watchlist_api

        db = SessionLocal()
        db.add(StrategyConfig(symbol="NVDA.US", market="US"))
        db.add_all(
            [
                WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=True, created_at=datetime(2026, 6, 4, 10, 1, tzinfo=timezone.utc)),
                WatchlistItem(symbol="AAPL.US", market="US", alias="Apple", is_active=False, created_at=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc)),
            ]
        )
        db.commit()
        db.close()

        class FakeBroker:
            def get_quotes(self, symbols):
                assert symbols == ["NVDA.US", "AAPL.US"]
                return [
                    SimpleNamespace(
                        symbol="NVDA.US",
                        last_price=Decimal("180.5"),
                        bid=Decimal("180.4"),
                        ask=Decimal("180.6"),
                        timestamp="2026-06-04T10:00:00Z",
                    ),
                    SimpleNamespace(
                        symbol="AAPL.US",
                        last_price=Decimal("199.5"),
                        bid=Decimal("199.4"),
                        ask=Decimal("199.6"),
                        timestamp=datetime(2026, 6, 4, 10, 0, tzinfo=timezone.utc),
                    ),
                ]

        monkeypatch.setattr(watchlist_api, "BrokerGateway", lambda: FakeBroker())

        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 200
        data = resp.json()
        assert data == [
            {
                "symbol": "NVDA.US",
                "market": "US",
                "alias": "Nvidia",
                "is_trading_target": True,
                "last_price": 180.5,
                "bid": 180.4,
                "ask": 180.6,
                "timestamp": "2026-06-04T10:00:00Z",
            },
            {
                "symbol": "AAPL.US",
                "market": "US",
                "alias": "Apple",
                "is_trading_target": False,
                "last_price": 199.5,
                "bid": 199.4,
                "ask": 199.6,
                "timestamp": "2026-06-04T10:00:00+00:00",
            },
        ]

    def test_get_watchlist_snapshots_returns_503_when_broker_fails(self, clean_db, monkeypatch):
        from app.api import watchlist as watchlist_api

        db = SessionLocal()
        db.add(WatchlistItem(symbol="NVDA.US", market="US", alias="Nvidia", is_active=False))
        db.commit()
        db.close()

        class FakeBroker:
            def get_quotes(self, symbols):
                raise RuntimeError("broker down")

        monkeypatch.setattr(watchlist_api, "BrokerGateway", lambda: FakeBroker())

        resp = client.get("/api/watchlist/snapshots")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "broker quote unavailable"

class TestWatchlistService:
    def test_list_items(self, clean_db):
        db = SessionLocal()
        svc = WatchlistService(db)
        svc.add_item(type("obj", (), {"symbol": "A.US", "market": "US", "alias": ""})())  # pyright: ignore
        items = svc.list_items()
        assert len(items) == 1
        db.close()

    def test_set_trading_symbol_single_active(self, clean_db):
        db = SessionLocal()
        svc = WatchlistService(db)
        item1 = svc.add_item(type("obj", (), {"symbol": "A.US", "market": "US", "alias": ""})())  # pyright: ignore
        item2 = svc.add_item(type("obj", (), {"symbol": "B.US", "market": "US", "alias": ""})())  # pyright: ignore

        svc.set_trading_symbol(item1.id)
        _item1 = svc.get_item(item1.id)
        assert _item1 is not None
        assert _item1.is_active is True
        _item2 = svc.get_item(item2.id)
        assert _item2 is not None
        assert _item2.is_active is False

        svc.set_trading_symbol(item2.id)
        _item1b = svc.get_item(item1.id)
        assert _item1b is not None
        assert _item1b.is_active is False
        _item2b = svc.get_item(item2.id)
        assert _item2b is not None
        assert _item2b.is_active is True
        db.close()