"""Closed round-trip trades API (GET /api/trades). Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import date, datetime, time, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_trades_api_{os.getpid()}.db"
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


class TestTradesAPI(_Base):
    def test_empty_returns_empty_page(self) -> None:
        resp = self.client.get("/api/trades")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_returns_closed_round_trip(self) -> None:
        db = self._db()
        db.add_all([
            self._order("buy", "AAPL.US", "BUY", 100, 10, date(2026, 1, 1), 10),
            self._order("sell", "AAPL.US", "SELL", 100, 12, date(2026, 1, 5), 11),
        ])
        db.commit()
        db.close()

        resp = self.client.get("/api/trades")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        row = data["items"][0]
        assert row["symbol"] == "AAPL.US"
        assert row["side"] == "long"
        assert row["entry_price"] == pytest.approx(10.0)
        assert row["exit_price"] == pytest.approx(12.0)
        assert row["quantity"] == pytest.approx(100.0)
        assert row["gross_pnl"] == pytest.approx(200.0)

    def test_symbol_filter(self) -> None:
        db = self._db()
        db.add_all([
            self._order("a-buy", "AAPL.US", "BUY", 10, 100, date(2026, 1, 1), 10),
            self._order("a-sell", "AAPL.US", "SELL", 10, 110, date(2026, 1, 1), 11),
            self._order("t-buy", "TSLA.US", "BUY", 10, 200, date(2026, 1, 1), 10),
            self._order("t-sell", "TSLA.US", "SELL", 10, 190, date(2026, 1, 1), 11),
        ])
        db.commit()
        db.close()

        resp = self.client.get("/api/trades", params={"symbol": "tsla.us"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["symbol"] == "TSLA.US"

    def test_date_filter_on_exit(self) -> None:
        db = self._db()
        db.add_all([
            self._order("e-buy", "AAPL.US", "BUY", 10, 100, date(2026, 1, 1), 9),
            self._order("e-sell", "AAPL.US", "SELL", 10, 110, date(2026, 1, 1), 11),
            self._order("l-buy", "AAPL.US", "BUY", 10, 100, date(2026, 2, 1), 9),
            self._order("l-sell", "AAPL.US", "SELL", 10, 110, date(2026, 2, 1), 11),
        ])
        db.commit()
        db.close()

        resp = self.client.get("/api/trades", params={"from_date": "2026-01-15", "to_date": "2026-02-28"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["exit_at"].startswith("2026-02-01")

    def test_limit_caps_but_total_reflects_full_count(self) -> None:
        db = self._db()
        for i in range(3):
            d = date(2026, 1, 1 + i)
            db.add_all([
                self._order(f"b{i}", "AAPL.US", "BUY", 10, 100, d, 9),
                self._order(f"s{i}", "AAPL.US", "SELL", 10, 110, d, 11),
            ])
        db.commit()
        db.close()

        resp = self.client.get("/api/trades", params={"limit": 2})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["total"] == 3

    def test_most_recent_exit_first(self) -> None:
        db = self._db()
        db.add_all([
            self._order("b0", "AAPL.US", "BUY", 10, 100, date(2026, 1, 1), 9),
            self._order("s0", "AAPL.US", "SELL", 10, 110, date(2026, 1, 1), 11),
            self._order("b1", "AAPL.US", "BUY", 10, 100, date(2026, 1, 2), 9),
            self._order("s1", "AAPL.US", "SELL", 10, 110, date(2026, 1, 2), 11),
        ])
        db.commit()
        db.close()

        data = self.client.get("/api/trades").json()
        exits = [item["exit_at"] for item in data["items"]]
        assert exits == sorted(exits, reverse=True)

    def test_fee_rate_read_from_config(self) -> None:
        db = self._db()
        db.add(StrategyConfig(fee_rate_us=0.001, fee_rate_hk=0.003))
        db.add_all([
            self._order("buy", "AAPL.US", "BUY", 100, 10, date(2026, 1, 1), 10),
            self._order("sell", "AAPL.US", "SELL", 100, 12, date(2026, 1, 1), 11),
        ])
        db.commit()
        db.close()

        row = self.client.get("/api/trades").json()["items"][0]
        expected_fee = (10 + 12) * 100 * 0.001  # 2.2
        assert row["est_fees"] == pytest.approx(expected_fee)
        assert row["net_pnl"] == pytest.approx(200.0 - expected_fee)

    def test_invalid_date_returns_422(self) -> None:
        resp = self.client.get("/api/trades", params={"from_date": "not-a-date"})
        assert resp.status_code == 422


class TestTradesStatsAPI(_Base):
    def test_empty_stats(self) -> None:
        resp = self.client.get("/api/trades/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total_trades"] == 0
        assert data["current_streak_type"] == "none"
        assert data["profit_factor"] is None

    def test_stats_with_mixed_trades(self) -> None:
        db = self._db()
        # A win and a loss, both today (within default 30-day window).
        today = date.today()
        db.add_all([
            self._order("b1", "AAPL.US", "BUY", 10, 100, today, 9),
            self._order("s1", "AAPL.US", "SELL", 10, 110, today, 10),  # +100
            self._order("b2", "AAPL.US", "BUY", 10, 100, today, 11),
            self._order("s2", "AAPL.US", "SELL", 10, 95, today, 12),   # -50
        ])
        db.commit()
        db.close()

        resp = self.client.get("/api/trades/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total_trades"] == 2
        assert data["win_count"] == 1
        assert data["loss_count"] == 1
        assert data["win_rate"] == pytest.approx(50.0)
        assert data["max_win_streak"] == 1
        assert data["max_loss_streak"] == 1
        # Profit factor uses take-home PnL after the frozen round-trip costs.
        assert data["profit_factor"] == pytest.approx(1.9411)

    def test_days_window_excludes_old_trades(self) -> None:
        db = self._db()
        old = date(2020, 1, 1)
        today = date.today()
        db.add_all([
            self._order("ob", "AAPL.US", "BUY", 10, 100, old, 9),
            self._order("os", "AAPL.US", "SELL", 10, 110, old, 10),
            self._order("tb", "AAPL.US", "BUY", 10, 100, today, 9),
            self._order("ts", "AAPL.US", "SELL", 10, 110, today, 10),
        ])
        db.commit()
        db.close()

        data = self.client.get("/api/trades/stats", params={"days": 30}).json()
        assert data["total_trades"] == 1  # only today's trade within 30 days


class TestTradesAnalyticsAPI(_Base):
    def _seed_trips(self) -> None:
        db = self._db()
        db.add_all([
            self._order("b1", "AAPL.US", "BUY", 10, 100, date(2026, 1, 5), 9),
            self._order("s1", "AAPL.US", "SELL", 10, 110, date(2026, 1, 5), 10),
            self._order("b2", "AAPL.US", "BUY", 10, 100, date(2026, 1, 7), 9),
            self._order("s2", "AAPL.US", "SELL", 10, 95, date(2026, 1, 7), 10),
            self._order("b3", "MSFT.US", "BUY", 10, 20, date(2026, 2, 2), 9),
            self._order("s3", "MSFT.US", "SELL", 10, 25, date(2026, 2, 2), 11),
        ])
        db.commit()
        db.close()

    def test_calendar_endpoint_returns_daily_rows(self) -> None:
        self._seed_trips()

        resp = self.client.get("/api/trades/analytics/calendar")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [row["date"] for row in data["items"]] == ["2026-01-05", "2026-01-07", "2026-02-02"]
        assert data["items"][0]["trade_count"] == 1
        assert data["items"][0]["net_pnl"] > 0

    def test_distribution_endpoint_supports_symbol_filter(self) -> None:
        self._seed_trips()

        resp = self.client.get("/api/trades/analytics/pnl-distribution", params={"symbol": "MSFT.US"})
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert sum(row["trade_count"] for row in data["items"]) == 1
        assert data["total_trades"] == 1

    def test_monthly_endpoint_returns_cumulative_rows(self) -> None:
        self._seed_trips()

        resp = self.client.get("/api/trades/analytics/monthly")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [row["month"] for row in data["items"]] == ["2026-01", "2026-02"]
        assert data["items"][1]["cumulative_pnl"] == data["total_net_pnl"]

    def test_weekday_endpoint_uses_exit_weekday(self) -> None:
        self._seed_trips()

        resp = self.client.get("/api/trades/analytics/weekday")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [row["label"] for row in data["items"]] == ["Mon", "Wed"]
        assert data["items"][0]["trade_count"] == 2

    def test_hold_duration_endpoint_returns_all_buckets(self) -> None:
        self._seed_trips()

        resp = self.client.get("/api/trades/analytics/hold-duration")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert [row["bucket"] for row in data["items"]] == ["<5m", "5m-1h", "1h-1d", "1d-1w", ">=1w"]
