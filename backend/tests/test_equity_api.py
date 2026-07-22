"""Equity curve API (GET /api/equity/curve). Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_equity_api_{os.getpid()}.db"
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
        # Zero fees so the curve reflects gross round-trip math (the fee
        # formula itself is covered in tests/test_trade_ledger.py).
        db.add(StrategyConfig(fee_rate_us=0.0, fee_rate_hk=0.0))
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _order(self, oid: str, symbol: str, side: str, qty: float, price: float, day: date, hour: int) -> OrderRecord:
        return OrderRecord(
            broker_order_id=oid,
            symbol=symbol,
            side=side,
            quantity=qty,
            price=price,
            executed_quantity=qty,
            executed_price=price,
            status="FILLED",
            created_at=_dt(day, hour),
            filled_at=_dt(day, hour, 1),
        )


class TestEquityCurveAPI(_Base):
    def test_empty_curve(self) -> None:
        resp = self.client.get("/api/equity/curve")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["points"] == []
        assert data["total_realized_pnl"] == 0.0
        assert data["max_drawdown"] == 0.0

    def test_cumulative_and_drawdown(self) -> None:
        db = self._db()
        base = date(2026, 1, 1)
        # day0 +100, day1 -50, day2 -30 -> cum 100,50,20 ; drawdown 0,50,80
        for i, sell_price in enumerate((11.0, 9.5, 9.7)):
            d = base.replace(day=1 + i)
            db.add_all([
                self._order(f"b{i}", "AAPL.US", "BUY", 100, 10.0, d, 9),
                self._order(f"s{i}", "AAPL.US", "SELL", 100, sell_price, d, 11),
            ])
        db.commit()
        db.close()

        resp = self.client.get("/api/equity/curve", params={"days": 365})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        cum = [p["cumulative_pnl"] for p in data["points"]]
        assert cum == [pytest.approx(100.0), pytest.approx(50.0), pytest.approx(20.0)]
        assert data["max_drawdown"] == pytest.approx(80.0)
        assert data["total_realized_pnl"] == pytest.approx(20.0)

    def test_account_wide_across_symbols(self) -> None:
        db = self._db()
        base = date(2026, 1, 1)
        db.add_all([
            self._order("ab", "AAPL.US", "BUY", 10, 100, base, 9),
            self._order("as", "AAPL.US", "SELL", 10, 110, base, 11),   # +100
            self._order("tb", "TSLA.US", "BUY", 10, 200, base, 9),
            self._order("ts", "TSLA.US", "SELL", 10, 215, base, 11),   # +150
        ])
        db.commit()
        db.close()

        data = self.client.get("/api/equity/curve", params={"days": 365}).json()
        # Both symbols' realized PnL land in the same day bucket (account-wide).
        assert len(data["points"]) == 1
        assert data["points"][0]["cumulative_pnl"] == pytest.approx(250.0)
        assert data["points"][0]["trade_count"] == 2

    def test_unresolved_exit_omits_complete_trade_on_same_market_day(self) -> None:
        db = self._db()
        day = date(2026, 7, 1)
        db.add_all([
            self._order("quality-buy", "AAPL.US", "BUY", 10, 100, day, 9),
            self._order(
                "quality-valid-sell", "AAPL.US", "SELL", 10, 110, day, 10
            ),
            self._order(
                "quality-unmatched-sell", "AAPL.US", "SELL", 1, 111, day, 11
            ),
        ])
        db.commit()
        db.close()

        response = self.client.get("/api/equity/curve", params={"days": 365})

        assert response.status_code == 200, response.text
        data = response.json()
        assert data["points"] == []
        assert data["total_realized_pnl"] == 0
        assert data["statistics_quality"]["status"] == "UNRESOLVED"
        assert data["statistics_quality"]["omitted_day_count"] == 1
