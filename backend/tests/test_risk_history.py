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


class TestRuntimeStateSnapshotPruning(_Base):
    def _seed_aged(self, symbol: str, ages_days: list[int]) -> None:
        now = datetime.now(timezone.utc)
        db = self._db()
        for age in ages_days:
            db.add(RuntimeStateSnapshot(
                symbol=symbol,
                engine_state="flat",
                daily_pnl=0.0,
                consecutive_losses=0,
                last_price=100.0,
                last_trigger_price=0.0,
                created_at=now - timedelta(days=age),
            ))
        db.commit()
        db.close()

    def _count(self, symbol: str | None = None) -> int:
        db = self._db()
        try:
            query = db.query(RuntimeStateSnapshot)
            if symbol is not None:
                query = query.filter(RuntimeStateSnapshot.symbol == symbol)
            return query.count()
        finally:
            db.close()

    def test_prunes_rows_older_than_retention(self) -> None:
        self._seed_aged("AAPL.US", [60, 45, 40, 5, 1])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=30,
            batch_size=100,
        )
        assert result.deleted == 3
        assert result.batches == 1
        assert self._count("AAPL.US") == 2

    def test_retains_latest_snapshot_per_symbol_even_when_expired(self) -> None:
        self._seed_aged("AAPL.US", [90, 80, 70])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=30,
            batch_size=100,
        )
        assert result.deleted == 2
        remaining = self._count("AAPL.US")
        assert remaining == 1
        resp = RiskHistoryService(self._db()).get_history(symbol="AAPL.US")
        assert resp.latest is not None

    def test_retention_zero_disables_pruning(self) -> None:
        self._seed_aged("AAPL.US", [365, 200])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=0,
            batch_size=100,
        )
        assert result.deleted == 0
        assert result.batches == 0
        assert self._count("AAPL.US") == 2

    def test_prunes_each_symbol_independently(self) -> None:
        self._seed_aged("AAPL.US", [60, 50])
        self._seed_aged("NVDA.US", [70, 2])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=30,
            batch_size=100,
        )
        assert result.deleted == 2
        assert self._count("AAPL.US") == 1
        assert self._count("NVDA.US") == 1

    def test_respects_batch_and_max_batches(self) -> None:
        self._seed_aged("AAPL.US", [60, 59, 58, 57, 56, 1])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=30,
            batch_size=2,
            max_batches=2,
        )
        assert result.deleted == 4
        assert result.batches == 2
        assert self._count("AAPL.US") == 2

    def test_invokes_lease_callbacks(self) -> None:
        self._seed_aged("AAPL.US", [60, 50])
        fenced: list[object] = []
        checkpoints: list[int] = []
        service = RiskHistoryService(
            self._db(),
            transaction_fence=lambda session: fenced.append(session),
            operation_checkpoint=lambda: checkpoints.append(1),
        )
        service.prune_expired_snapshots(retention_days=30, batch_size=100)
        assert fenced
        assert checkpoints

    def test_rejects_invalid_arguments(self) -> None:
        service = RiskHistoryService(self._db())
        try:
            service.prune_expired_snapshots(retention_days=-1, batch_size=10)
            raise AssertionError("negative retention must raise")
        except ValueError:
            pass
        try:
            service.prune_expired_snapshots(retention_days=30, batch_size=0)
            raise AssertionError("non-positive batch must raise")
        except ValueError:
            pass

    def test_noop_when_nothing_expired(self) -> None:
        self._seed_aged("AAPL.US", [3, 2, 1])
        result = RiskHistoryService(self._db()).prune_expired_snapshots(
            retention_days=30,
            batch_size=100,
        )
        assert result.deleted == 0
        assert self._count("AAPL.US") == 3
