"""Daily risk history — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timedelta, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_risk_history_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, RuntimeStateSnapshot
from app.services.risk_history_service import RiskHistoryService


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False}
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(RuntimeStateSnapshot).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _seed(self) -> None:
        base = datetime(2026, 6, 16, 10, 0, tzinfo=timezone.utc)
        db = self._db()
        for i, pnl in enumerate([-100, -50, 80, 120, -200]):
            db.add(RuntimeStateSnapshot(
                symbol="AAPL.US",
                engine_state="flat",
                paused=(pnl == -200),
                kill_switch=False,
                daily_pnl=float(pnl),
                consecutive_losses=2 if pnl < 0 else 0,
                last_price=150.0,
                last_trigger_price=0.0,
                created_at=base + timedelta(minutes=i),
            ))
        db.commit()
        db.close()


class TestRiskHistoryService(_Base):
    def test_history_chronological_with_latest(self) -> None:
        self._seed()
        resp = RiskHistoryService(self._db()).get_history(symbol="AAPL.US", limit=100)
        assert len(resp.points) == 5
        # Chronological: first point is the oldest (-100), last is latest (-200).
        assert resp.points[0].daily_pnl == -100
        assert resp.points[-1].daily_pnl == -200
        assert resp.latest is not None
        assert resp.latest.daily_pnl == -200
        assert resp.latest.paused is True

    def test_empty(self) -> None:
        resp = RiskHistoryService(self._db()).get_history()
        assert resp.points == []
        assert resp.latest is None

    def test_limit_cap(self) -> None:
        self._seed()
        resp = RiskHistoryService(self._db()).get_history(limit=2)
        assert len(resp.points) == 2  # only the 2 most recent


class TestRiskHistoryAPI(_Base):
    def test_endpoint(self) -> None:
        self._seed()
        resp = self.client.get("/api/risk/history", params={"symbol": "AAPL.US", "limit": 10})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["points"]) == 5
        assert data["latest"]["daily_pnl"] == -200

    def test_endpoint_empty(self) -> None:
        resp = self.client.get("/api/risk/history")
        assert resp.status_code == 200
        assert resp.json()["points"] == []
