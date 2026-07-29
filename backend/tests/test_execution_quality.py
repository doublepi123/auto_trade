"""Execution quality analytics (GET /api/execution-quality/*). Per-file sqlite."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Any

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_exec_quality_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, TradeEvent
from app.services.execution_quality_service import ExecutionQualityService


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _event(
    *,
    event_type: str,
    symbol: str = "AAPL.US",
    broker_order_id: str = "ord-1",
    status: str = "",
    message: str = "",
    payload: dict[str, Any] | None = None,
    created_at: datetime | None = None,
) -> TradeEvent:
    return TradeEvent(
        event_type=event_type,
        symbol=symbol,
        broker_order_id=broker_order_id,
        status=status,
        message=message,
        payload_json=json.dumps(payload or {}),
        created_at=created_at or _now(),
    )


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
        db.query(TradeEvent).delete()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _add(self, *events: TradeEvent) -> None:
        db = self._db()
        for ev in events:
            db.add(ev)
        db.commit()
        db.close()


class TestEmptySummary(_Base):
    def test_empty_db_returns_zero_summary(self) -> None:
        svc = ExecutionQualityService(self._db())
        summary = svc.get_quality_summary(days=30)
        assert summary["total_orders"] == 0
        assert summary["filled_orders"] == 0
        assert summary["fill_rate_pct"] == 0.0
        assert summary["rejection_rate_pct"] == 0.0
        assert summary["rejection_reasons"] == {}
        assert summary["by_symbol"] == {}

    def test_empty_db_endpoints_return_ok(self) -> None:
        summary = self.client.get("/api/execution-quality/summary")
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["total_orders"] == 0
        assert body["by_symbol"] == {}

        slip = self.client.get("/api/execution-quality/slippage")
        assert slip.status_code == 200, slip.text
        assert slip.json() == []


class TestFillRate(_Base):
    def test_fill_rate_with_submit_and_fill_events(self) -> None:
        base = _now() - timedelta(minutes=10)
        # 2 submits; 1 of them fills, the other is rejected.
        self._add(
            _event(
                event_type="ORDER_SUBMITTED", broker_order_id="ord-fill",
                payload={"submit_at": base.isoformat()},
                created_at=base,
            ),
            _event(
                event_type="ORDER_FILLED", broker_order_id="ord-fill",
                payload={"fill_at": (base + timedelta(seconds=2)).isoformat()},
                created_at=base + timedelta(seconds=2),
            ),
            _event(
                event_type="ORDER_SUBMITTED", broker_order_id="ord-rej",
                payload={"submit_at": (base + timedelta(minutes=1)).isoformat()},
                created_at=base + timedelta(minutes=1),
            ),
            _event(
                event_type="ORDER_REJECTED", broker_order_id="ord-rej",
                payload={"reason": "INSUFFICIENT_BUYING_POWER"},
                message="order rejected by broker",
                created_at=base + timedelta(minutes=1, seconds=5),
            ),
        )
        svc = ExecutionQualityService(self._db())
        summary = svc.get_quality_summary(days=30)
        assert summary["total_orders"] == 4
        assert summary["filled_orders"] == 1
        assert summary["rejected_orders"] == 1
        # 1 fill out of 4 events = 25 %.
        assert summary["fill_rate_pct"] == 25.0
        # 1 reject out of 4 events = 25 %.
        assert summary["rejection_rate_pct"] == 25.0
        # submit->fill latency = 2 seconds.
        assert summary["avg_fill_time_seconds"] == 2.0
        # Rejection reason surfaced from payload.
        assert summary["rejection_reasons"] == {"INSUFFICIENT_BUYING_POWER": 1}

    def test_by_symbol_breakdown(self) -> None:
        self._add(
            _event(event_type="ORDER_SUBMITTED", symbol="AAPL.US", broker_order_id="a1"),
            _event(event_type="ORDER_FILLED", symbol="AAPL.US", broker_order_id="a1"),
            _event(event_type="ORDER_SUBMITTED", symbol="TSLA.US", broker_order_id="t1"),
            _event(event_type="ORDER_CANCELLED", symbol="TSLA.US", broker_order_id="t1"),
        )
        summary = ExecutionQualityService(self._db()).get_quality_summary(days=30)
        assert summary["by_symbol"]["AAPL.US"]["orders"] == 2
        assert summary["by_symbol"]["AAPL.US"]["fills"] == 1
        assert summary["by_symbol"]["TSLA.US"]["rejects"] == 0
        assert summary["cancelled_orders"] == 1

    def test_trailing_ed_event_spellings_accepted(self) -> None:
        """ORDER_REJECT / ORDER_CANCEL (bare) must count the same as -ED forms."""
        self._add(
            _event(event_type="ORDER_SUBMIT", broker_order_id="x1"),
            _event(event_type="ORDER_FILL", broker_order_id="x1"),
            _event(event_type="ORDER_REJECT", broker_order_id="x2"),
            _event(event_type="ORDER_CANCEL", broker_order_id="x3"),
        )
        summary = ExecutionQualityService(self._db()).get_quality_summary(days=30)
        assert summary["filled_orders"] == 1
        assert summary["rejected_orders"] == 1
        assert summary["cancelled_orders"] == 1

    def test_days_window_excludes_old_events(self) -> None:
        old = _now() - timedelta(days=40)
        self._add(
            _event(event_type="ORDER_SUBMITTED", broker_order_id="old", created_at=old),
        )
        summary = ExecutionQualityService(self._db()).get_quality_summary(days=30)
        assert summary["total_orders"] == 0

    def test_summary_endpoint_validates_typed_by_symbol(self) -> None:
        self._add(
            _event(event_type="ORDER_SUBMITTED", symbol="AAPL.US", broker_order_id="a1"),
            _event(event_type="ORDER_FILLED", symbol="AAPL.US", broker_order_id="a1"),
        )
        resp = self.client.get("/api/execution-quality/summary")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["by_symbol"]["AAPL.US"]["fills"] == 1
        assert body["fill_rate_pct"] == 50.0


class TestSlippage(_Base):
    def test_slippage_from_persisted_bps(self) -> None:
        """OrderRecord.slippage_bps (basis points) is the authoritative source."""
        db = self._db()
        db.add(OrderRecord(
            broker_order_id="o1", symbol="AAPL.US", side="BUY",
            quantity=100, price=10.0, executed_quantity=100, executed_price=10.02,
            status="FILLED", filled_at=_now(),
            slippage_bps=20.0,  # +20 bps = +0.20 %
        ))
        db.commit()
        db.close()
        rows = ExecutionQualityService(self._db()).get_slippage_analysis(days=30)
        assert len(rows) == 1
        row = rows[0]
        assert row["symbol"] == "AAPL.US"
        # 20 bps / 100 = 0.20 % expressed as the fraction 0.0020.
        assert abs(row["avg_slippage_pct"] - 0.002) < 1e-9
        assert row["direction_bias"] == "favorable"
        assert row["trade_count"] == 1

    def test_slippage_from_decision_mid_when_no_bps(self) -> None:
        db = self._db()
        db.add(OrderRecord(
            broker_order_id="o2", symbol="TSLA.US", side="BUY",
            quantity=100, price=20.0, executed_quantity=100, executed_price=20.10,
            status="FILLED", filled_at=_now(),
            decision_bid=19.90, decision_ask=20.10,  # mid = 20.00
        ))
        db.commit()
        db.close()
        rows = ExecutionQualityService(self._db()).get_slippage_analysis(days=30)
        assert len(rows) == 1
        # (20.10 - 20.00) / 20.00 = 0.005 = +0.5 %.
        assert abs(rows[0]["avg_slippage_pct"] - 0.005) < 1e-9
        assert rows[0]["direction_bias"] == "favorable"

    def test_adverse_slippage_bias(self) -> None:
        db = self._db()
        db.add(OrderRecord(
            broker_order_id="o3", symbol="MSFT.US", side="BUY",
            quantity=100, price=30.0, executed_quantity=100, executed_price=29.85,
            status="FILLED", filled_at=_now(),
            decision_bid=30.00, decision_ask=30.00,  # mid = 30.00
        ))
        db.commit()
        db.close()
        rows = ExecutionQualityService(self._db()).get_slippage_analysis(days=30)
        assert rows[0]["direction_bias"] == "adverse"
        assert rows[0]["avg_slippage_pct"] < 0

    def test_slippage_aggregates_per_symbol(self) -> None:
        db = self._db()
        now = _now()
        for i, bps in enumerate((10.0, 30.0)):
            db.add(OrderRecord(
                broker_order_id=f"o{i}", symbol="AAPL.US", side="BUY",
                quantity=100, price=10.0, executed_quantity=100,
                executed_price=10.0 + bps / 1e4, status="FILLED", filled_at=now,
                slippage_bps=bps,
            ))
        db.add(OrderRecord(
            broker_order_id="o9", symbol="TSLA.US", side="BUY",
            quantity=100, price=20.0, executed_quantity=100, executed_price=20.05,
            status="FILLED", filled_at=now,
            decision_bid=20.00, decision_ask=20.00,
        ))
        db.commit()
        db.close()
        rows = ExecutionQualityService(self._db()).get_slippage_analysis(days=30)
        by_sym = {r["symbol"]: r for r in rows}
        assert by_sym["AAPL.US"]["trade_count"] == 2
        # avg of +10bps and +30bps = +20bps -> 0.0020 fraction.
        assert abs(by_sym["AAPL.US"]["avg_slippage_pct"] - 0.002) < 1e-9
        # max slippage is the most positive = 30 bps -> 0.0030.
        assert abs(by_sym["AAPL.US"]["max_slippage_pct"] - 0.003) < 1e-9
        assert by_sym["TSLA.US"]["trade_count"] == 1

    def test_slippage_falls_back_to_event_payload(self) -> None:
        """When no filled OrderRecord exists, use signal/fill from fill payload."""
        self._add(_event(
            event_type="ORDER_FILLED", symbol="SPY.US", broker_order_id="p1",
            payload={"signal_price": 400.0, "fill_price": 400.8},
        ))
        rows = ExecutionQualityService(self._db()).get_slippage_analysis(days=30)
        assert len(rows) == 1
        assert rows[0]["symbol"] == "SPY.US"
        # (400.8 - 400) / 400 = 0.002 = +0.2 %.
        assert abs(rows[0]["avg_slippage_pct"] - 0.002) < 1e-9

    def test_slippage_endpoint(self) -> None:
        db = self._db()
        db.add(OrderRecord(
            broker_order_id="e1", symbol="AAPL.US", side="BUY",
            quantity=100, price=10.0, executed_quantity=100, executed_price=10.01,
            status="FILLED", filled_at=_now(),
            decision_bid=10.00, decision_ask=10.00,
        ))
        db.commit()
        db.close()
        resp = self.client.get("/api/execution-quality/slippage")
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["symbol"] == "AAPL.US"
        assert rows[0]["direction_bias"] == "favorable"