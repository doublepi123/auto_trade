"""Live unrealized PnL (positions) — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_positions_pnl_{os.getpid()}.db"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, TrackedEntry
from app.services.position_pnl_service import PositionPnlService


class FakeQuote:
    def __init__(self, symbol: str, last_price: float) -> None:
        self.symbol = symbol
        self.last_price = last_price


class FakeBroker:
    def __init__(self, quotes: dict[str, float]) -> None:
        self._quotes = quotes
        self.raising = False

    def get_quotes(self, symbols: list[str]) -> list[FakeQuote]:
        if self.raising:
            raise RuntimeError("broker down")
        return [FakeQuote(s, self._quotes[s]) for s in symbols if s in self._quotes]


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
        db.query(TrackedEntry).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _add(self, symbol: str, quantity: float, cost: float) -> None:
        db = self._db()
        db.add(TrackedEntry(symbol=symbol, quantity=quantity, cost=cost))
        db.commit()
        db.close()


class TestPositionPnlService(_Base):
    def test_long_position_pnl(self) -> None:
        self._add("AAPL.US", 10, 1000.0)
        result = PositionPnlService(self._db(), FakeBroker({"AAPL.US": 120.0})).get_positions_pnl()
        assert len(result.positions) == 1
        row = result.positions[0]
        assert row.quantity == 10
        assert row.avg_entry_cost == 100.0
        assert row.last_price == 120.0
        assert row.unrealized_pnl == pytest.approx(200.0)
        assert row.unrealized_pnl_pct == pytest.approx(20.0)

    def test_short_position_pnl(self) -> None:
        self._add("AAPL.US", -10, 1000.0)
        result = PositionPnlService(self._db(), FakeBroker({"AAPL.US": 80.0})).get_positions_pnl()
        row = result.positions[0]
        # short: profit when price falls below cost
        assert row.unrealized_pnl == pytest.approx(200.0)
        assert row.unrealized_pnl_pct == pytest.approx(20.0)

    def test_no_quote_provider(self) -> None:
        self._add("AAPL.US", 10, 1000.0)
        result = PositionPnlService(self._db(), None).get_positions_pnl()
        row = result.positions[0]
        assert row.has_quote is False
        assert row.last_price is None
        assert row.unrealized_pnl == 0.0
        assert row.unrealized_pnl_pct is None
        assert row.cost_value == pytest.approx(1000.0)
        assert result.total_cost_basis == pytest.approx(1000.0)

    def test_empty_positions(self) -> None:
        result = PositionPnlService(self._db(), FakeBroker({})).get_positions_pnl()
        assert result.positions == []
        assert result.total_unrealized_pnl == 0.0

    def test_zero_quantity_excluded(self) -> None:
        self._add("AAPL.US", 0, 100.0)
        self._add("MSFT.US", 5, 1000.0)
        result = PositionPnlService(self._db(), FakeBroker({"MSFT.US": 210.0})).get_positions_pnl()
        assert len(result.positions) == 1
        assert result.positions[0].symbol == "MSFT.US"

    def test_totals_and_unavailable_on_quote_failure(self) -> None:
        self._add("AAPL.US", 10, 1000.0)
        self._add("MSFT.US", 5, 1000.0)
        broker = FakeBroker({"AAPL.US": 120.0, "MSFT.US": 180.0})
        broker.raising = True
        result = PositionPnlService(self._db(), broker).get_positions_pnl()
        assert result.available is False
        assert result.total_cost_basis == pytest.approx(2000.0)
        assert result.total_unrealized_pnl == 0.0  # no quotes -> 0


class TestPositionPnlAPI(_Base):
    def test_endpoint_returns_positions(self, monkeypatch) -> None:
        self._add("AAPL.US", 10, 1000.0)

        class FakeRunner:
            def __init__(self) -> None:
                self.broker = FakeBroker({"AAPL.US": 130.0})

        monkeypatch.setattr("app.api.positions.get_runner", lambda: FakeRunner())
        resp = self.client.get("/api/positions/pnl")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["positions"][0]["unrealized_pnl"] == pytest.approx(300.0)
        assert data["total_unrealized_pnl"] == pytest.approx(300.0)
