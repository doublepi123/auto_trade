"""Per-symbol PnL attribution API (GET /api/pnl/by-symbol). Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_pnl_attr_api_{os.getpid()}.db"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig


def _dt(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


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
        db.query(OrderRecord).delete()
        db.query(StrategyConfig).delete()
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _roundtrip(self, oid_b: str, oid_s: str, symbol: str, buy_price: float, sell_price: float, qty: float, day: date) -> tuple[OrderRecord, OrderRecord]:
        return (
            OrderRecord(broker_order_id=oid_b, symbol=symbol, side="BUY", quantity=qty, price=buy_price,
                        executed_quantity=qty, executed_price=buy_price, status="FILLED",
                        created_at=_dt(day, 9), filled_at=_dt(day, 9, 1)),
            OrderRecord(broker_order_id=oid_s, symbol=symbol, side="SELL", quantity=qty, price=sell_price,
                        executed_quantity=qty, executed_price=sell_price, status="FILLED",
                        created_at=_dt(day, 11), filled_at=_dt(day, 11, 1)),
        )


class TestPnlBySymbolAPI(_Base):
    def test_empty(self) -> None:
        resp = self.client.get("/api/pnl/by-symbol")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["rows"] == []
        assert data["total_realized_pnl"] == 0.0

    def test_groups_by_symbol_sorted_by_abs_pnl(self) -> None:
        db = self._db()
        today = date.today()
        # TSLA +200 (qty10, 200->220), AAPL +50 (10,100->105), MSFT -120 (10,300->288)
        for b, s in [
            self._roundtrip("tb1", "ts1", "TSLA.US", 200.0, 220.0, 10, today),
            self._roundtrip("ab1", "as1", "AAPL.US", 100.0, 105.0, 10, today),
            self._roundtrip("mb1", "ms1", "MSFT.US", 300.0, 288.0, 10, today),
        ]:
            db.add_all([b, s])
        db.commit()
        db.close()

        data = self.client.get("/api/pnl/by-symbol", params={"days": 30}).json()
        symbols = [row["symbol"] for row in data["rows"]]
        assert symbols == ["TSLA.US", "MSFT.US", "AAPL.US"]
        assert data["total_realized_pnl"] == pytest.approx(130.0)
        by = {row["symbol"]: row for row in data["rows"]}
        assert by["MSFT.US"]["realized_pnl"] == pytest.approx(-120.0)
        assert by["MSFT.US"]["win_rate"] == pytest.approx(0.0)
        assert by["TSLA.US"]["contribution_share"] == pytest.approx(200.0 / 130.0, abs=1e-3)

    def test_symbol_filter(self) -> None:
        db = self._db()
        today = date.today()
        db.add_all(self._roundtrip("ab", "as", "AAPL.US", 100.0, 110.0, 10, today))
        db.add_all(self._roundtrip("tb", "ts", "TSLA.US", 200.0, 210.0, 10, today))
        db.commit()
        db.close()

        data = self.client.get("/api/pnl/by-symbol", params={"symbol": "tsla.us"}).json()
        assert len(data["rows"]) == 1
        assert data["rows"][0]["symbol"] == "TSLA.US"
