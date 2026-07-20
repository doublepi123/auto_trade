"""Closed round-trip trades export API (GET /api/trades/export)."""
from __future__ import annotations

import csv
import io
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, time, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, StrategyConfig


TEST_DATABASE_URL = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_trades_export_{os.getpid()}.db"
)


CSV_COLUMNS = [
    "symbol",
    "side",
    "entry_order_id",
    "exit_order_id",
    "entry_at",
    "exit_at",
    "entry_price",
    "exit_price",
    "quantity",
    "gross_pnl",
    "est_fees",
    "net_pnl",
    "holding_seconds",
    "fee_source",
    "actual_fees",
    "slippage_amount",
    "slippage_bps",
    "ack_latency_ms",
    "fill_latency_ms",
    "exit_cause",
    "exit_reason",
    "mfe_amount",
    "mae_amount",
    "mfe_pct",
    "mae_pct",
]


def _dt(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class _TradeSeed:
    prefix: str
    symbol: str
    day: date
    entry_price: float = 100
    exit_price: float = 110


class TestTradesExportAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
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
        cls.engine.dispose()

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            db.query(OrderRecord).delete()
            db.query(StrategyConfig).delete()
            db.commit()

    def _seed_trip(self, seed: _TradeSeed) -> None:
        with Session(bind=self.engine) as db:
            db.add_all([
                OrderRecord(
                    broker_order_id=f"{seed.prefix}-buy",
                    symbol=seed.symbol,
                    side="BUY",
                    quantity=10,
                    price=seed.entry_price,
                    executed_quantity=10,
                    executed_price=seed.entry_price,
                    status="FILLED",
                    created_at=_dt(seed.day, 9),
                    filled_at=_dt(seed.day, 9, 1),
                ),
                OrderRecord(
                    broker_order_id=f"{seed.prefix}-sell",
                    symbol=seed.symbol,
                    side="SELL",
                    quantity=10,
                    price=seed.exit_price,
                    executed_quantity=10,
                    executed_price=seed.exit_price,
                    status="FILLED",
                    created_at=_dt(seed.day, 11),
                    filled_at=_dt(seed.day, 11, 1),
                ),
            ])
            db.commit()

    def test_csv_matches_list_row_for_identical_filters(self) -> None:
        self._seed_trip(_TradeSeed("aapl", "AAPL.US", date(2026, 1, 5)))
        list_row = self.client.get("/api/trades", params={"symbol": "AAPL.US"}).json()["items"][0]

        response = self.client.get(
            "/api/trades/export",
            params={"symbol": "AAPL.US"},
        )

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("text/csv")
        assert response.headers["content-disposition"].startswith(
            'attachment; filename="trades_'
        )
        rows = list(csv.DictReader(io.StringIO(response.text)))
        assert rows[0].keys() == dict.fromkeys(CSV_COLUMNS).keys()
        assert rows[0]["symbol"] == list_row["symbol"]
        assert rows[0]["side"] == list_row["side"]
        assert int(rows[0]["entry_order_id"]) == list_row["entry_order_id"]
        assert int(rows[0]["exit_order_id"]) == list_row["exit_order_id"]
        assert float(rows[0]["gross_pnl"]) == list_row["gross_pnl"]
        assert float(rows[0]["est_fees"]) == list_row["est_fees"]
        assert float(rows[0]["net_pnl"]) == list_row["net_pnl"]

    def test_json_matches_list_items_for_identical_filters(self) -> None:
        self._seed_trip(_TradeSeed("old", "AAPL.US", date(2026, 1, 5)))
        self._seed_trip(_TradeSeed("new", "AAPL.US", date(2026, 2, 5)))
        params = {
            "format": "json",
            "symbol": "AAPL.US",
            "from_date": "2026-02-01",
            "to_date": "2026-02-28",
        }
        list_response = self.client.get("/api/trades", params=params)

        response = self.client.get("/api/trades/export", params=params)

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert response.json() == list_response.json()["items"]

    def test_symbol_filter_excludes_other_symbols(self) -> None:
        self._seed_trip(_TradeSeed("aapl", "AAPL.US", date(2026, 1, 5)))
        self._seed_trip(_TradeSeed("msft", "MSFT.US", date(2026, 1, 6)))

        response = self.client.get(
            "/api/trades/export",
            params={"format": "json", "symbol": "msft.us"},
        )

        assert [row["symbol"] for row in response.json()] == ["MSFT.US"]

    def test_date_filters_apply_to_exit_time(self) -> None:
        self._seed_trip(_TradeSeed("jan", "AAPL.US", date(2026, 1, 5)))
        self._seed_trip(_TradeSeed("feb", "AAPL.US", date(2026, 2, 5)))

        response = self.client.get(
            "/api/trades/export",
            params={
                "format": "json",
                "from_date": "2026-02-01",
                "to_date": "2026-02-28",
            },
        )

        assert len(response.json()) == 1
        assert response.json()[0]["exit_at"].startswith("2026-02-05")

    def test_empty_csv_contains_only_header(self) -> None:
        response = self.client.get("/api/trades/export")

        assert response.status_code == 200, response.text
        assert response.text.splitlines() == [",".join(CSV_COLUMNS)]

    def test_invalid_format_returns_422(self) -> None:
        response = self.client.get("/api/trades/export", params={"format": "xml"})

        assert response.status_code == 422

    def test_invalid_date_returns_422(self) -> None:
        response = self.client.get(
            "/api/trades/export",
            params={"from_date": "not-a-date"},
        )

        assert response.status_code == 422

    def test_limit_caps_newest_first_export(self) -> None:
        for index in range(3):
            self._seed_trip(
                _TradeSeed(
                    f"trip-{index}",
                    "AAPL.US",
                    date(2026, 1, index + 1),
                )
            )

        response = self.client.get(
            "/api/trades/export",
            params={"format": "json", "limit": 2},
        )

        rows = response.json()
        assert len(rows) == 2
        assert [row["exit_at"][:10] for row in rows] == ["2026-01-03", "2026-01-02"]

    def test_limit_above_export_cap_returns_422(self) -> None:
        response = self.client.get(
            "/api/trades/export",
            params={"limit": 10001},
        )

        assert response.status_code == 422
