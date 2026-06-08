from __future__ import annotations

import os
from datetime import date, datetime, time, timezone

os.makedirs("data", exist_ok=True)
os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///data/test_reports_api_{os.getpid()}.db"

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord


class TestReportsApi:
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

    def _cleanup(self) -> None:
        db = Session(bind=self.engine)
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    @staticmethod
    def _dt(day: date, hour: int, minute: int = 0) -> datetime:
        return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)

    def _seed_roundtrip(self) -> None:
        db = Session(bind=self.engine)
        db.add_all(
            [
                OrderRecord(
                    broker_order_id="aapl-buy-1",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=2,
                    price=100,
                    executed_quantity=2,
                    executed_price=100,
                    status="FILLED",
                    created_at=self._dt(date(2026, 6, 1), 14),
                    filled_at=self._dt(date(2026, 6, 1), 14, 1),
                ),
                OrderRecord(
                    broker_order_id="aapl-sell-1",
                    symbol="AAPL.US",
                    side="SELL",
                    quantity=2,
                    price=105,
                    executed_quantity=2,
                    executed_price=105,
                    status="FILLED",
                    created_at=self._dt(date(2026, 6, 1), 15),
                    filled_at=self._dt(date(2026, 6, 1), 15, 1),
                ),
            ]
        )
        db.commit()
        db.close()

    def test_range_report_schema_and_metrics(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        response = self.client.get(
            "/api/reports/range",
            params={"symbol": "AAPL.US", "from_date": "2026-06-01", "to_date": "2026-06-01"},
        )

        assert response.status_code == 200
        payload = response.json()
        assert payload["metrics"]["total_pnl"] == 10.0
        assert payload["metrics"]["max_drawdown"] == 0.0
        assert payload["daily_points"][0]["cumulative_pnl"] == 10.0
        assert len(payload["attribution"]) == 1
        assert payload["attribution"][0]["key"] == "SELL"
        assert payload["attribution"][0]["trade_count"] == 1
        assert payload["attribution"][0]["pnl"] == 10.0
        assert len(payload["details"]) == 1
        assert payload["details"][0]["date"] == "2026-06-01"
        assert len(payload["details"][0]["orders"]) == 1
        assert payload["details"][0]["orders"][0]["side"] == "SELL"

    def test_range_report_normalizes_symbol_and_rejects_invalid_symbol(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        normalized = self.client.get(
            "/api/reports/range",
            params={"symbol": " aapl.us ", "from_date": "2026-06-01", "to_date": "2026-06-01"},
        )
        invalid = self.client.get(
            "/api/reports/range",
            params={"symbol": "AAPL", "from_date": "2026-06-01", "to_date": "2026-06-01"},
        )

        assert normalized.status_code == 200
        assert normalized.json()["symbol"] == "AAPL.US"
        assert normalized.json()["metrics"]["total_pnl"] == 10.0
        assert invalid.status_code == 400

    def test_range_report_rejects_unsupported_market_suffix(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        response = self.client.get(
            "/api/reports/range",
            params={"symbol": "7203.JP", "from_date": "2026-06-01", "to_date": "2026-06-01"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "symbol market must be US or HK"

    def test_daily_weekly_monthly_endpoints_return_200(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        daily = self.client.get(
            "/api/reports/daily",
            params={"symbol": "AAPL.US", "date": "2026-06-01"},
        )
        weekly = self.client.get(
            "/api/reports/weekly",
            params={"symbol": "AAPL.US", "week_start": "2026-06-01"},
        )
        monthly = self.client.get(
            "/api/reports/monthly",
            params={"symbol": "AAPL.US", "month": "2026-06"},
        )

        assert daily.status_code == 200
        assert weekly.status_code == 200
        assert monthly.status_code == 200

    def test_export_json_and_csv(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        json_response = self.client.get(
            "/api/reports/export",
            params={
                "symbol": "AAPL.US",
                "from_date": "2026-06-01",
                "to_date": "2026-06-01",
                "format": "json",
            },
        )
        csv_response = self.client.get(
            "/api/reports/export",
            params={
                "symbol": "AAPL.US",
                "from_date": "2026-06-01",
                "to_date": "2026-06-01",
                "format": "csv",
            },
        )

        assert json_response.status_code == 200
        assert json_response.headers["content-type"].startswith("application/json")
        assert csv_response.status_code == 200
        assert csv_response.headers["content-type"].startswith("text/csv")
        assert "cumulative_pnl" in csv_response.text
        assert csv_response.text.splitlines()[0] == "date,symbol,trade_count,win_count,pnl,cumulative_pnl,drawdown"

    def test_export_invalid_format_returns_400(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        response = self.client.get(
            "/api/reports/export",
            params={
                "symbol": "AAPL.US",
                "from_date": "2026-06-01",
                "to_date": "2026-06-01",
                "format": "xml",
            },
        )

        assert response.status_code == 400

    def test_invalid_date_returns_400(self) -> None:
        self._cleanup()

        response = self.client.get(
            "/api/reports/range",
            params={"symbol": "AAPL.US", "from_date": "2026-06-31", "to_date": "2026-06-01"},
        )

        assert response.status_code == 400

    def test_from_date_after_to_date_returns_400(self) -> None:
        self._cleanup()
        self._seed_roundtrip()

        response = self.client.get(
            "/api/reports/range",
            params={"symbol": "AAPL.US", "from_date": "2026-06-02", "to_date": "2026-06-01"},
        )

        assert response.status_code == 400
        assert response.json()["detail"] == "to_date must be greater than or equal to from_date"
