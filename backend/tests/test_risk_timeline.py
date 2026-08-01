"""Risk check timeline — service + API. Per-module sqlite."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Generator

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.api.risk_timeline import router
from app.config import settings
from app.database import get_db
from app.models import Base, TradeEvent
from app.services.risk_timeline_service import RiskTimelineService


_NOW = datetime.now(timezone.utc)


def _now_utc() -> datetime:
    """Current UTC timestamp for trailing-window summary tests.

    ``RiskTimelineService.get_risk_summary`` filters on a trailing window anchored
    to ``datetime.now(timezone.utc)``, so events that omit ``created_at`` must
    land inside that window regardless of when the suite runs. ``_NOW`` is kept
    only for tests where an explicit, deterministic chronology is under test.
    """
    return datetime.now(timezone.utc)


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(
            autocommit=False, autoflush=False, bind=cls.engine,
        )
        cls.app = FastAPI()
        cls.app.include_router(router)

        def override_get_db() -> Generator[Session, None, None]:
            db = cls.session_factory()
            try:
                yield db
            finally:
                db.close()

        cls.app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(cls.app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        cls.app.dependency_overrides.clear()
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        with self.session_factory() as db:
            db.query(TradeEvent).delete()
            db.commit()

    def _db(self) -> Session:
        return self.session_factory()

    def _add_event(
        self,
        *,
        event_type: str,
        symbol: str = "AAPL.US",
        broker_order_id: str = "",
        side: str = "BUY",
        status: str = "",
        message: str = "",
        payload: dict | None = None,
        created_at: datetime | None = None,
    ) -> TradeEvent:
        event = TradeEvent(
            event_type=event_type,
            symbol=symbol,
            broker_order_id=broker_order_id,
            side=side,
            status=status,
            message=message,
            payload_json=json.dumps(payload or {}),
            created_at=created_at or _now_utc(),
        )
        db = self._db()
        db.add(event)
        db.commit()
        db.refresh(event)
        db.close()
        return event


class TestRiskTimelineEmpty(_Base):
    def test_empty_db_returns_empty_checks(self) -> None:
        checks = RiskTimelineService(self._db()).get_trade_risk_checks()
        assert checks == []

    def test_empty_db_returns_zero_summary(self) -> None:
        summary = RiskTimelineService(self._db()).get_risk_summary()
        assert summary["total_checks"] == 0
        assert summary["passed"] == 0
        assert summary["blocked"] == 0
        assert summary["by_category"] == {
            "FEE": 0,
            "REPRICING": 0,
            "COOLDOWN": 0,
            "RISK": 0,
            "PENDING": 0,
            "POSITION": 0,
            "SESSION": 0,
        }
        assert summary["recent_blocks"] == []

    def test_api_checks_endpoint_empty(self) -> None:
        resp = self.client.get("/api/risk-timeline/checks")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_api_summary_endpoint_empty(self) -> None:
        resp = self.client.get("/api/risk-timeline/summary")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_checks"] == 0


class TestRiskTimelineChecks(_Base):
    def test_only_risk_relevant_events_returned(self) -> None:
        # Relevant: ORDER_SKIPPED + ORDER_SUBMITTED.
        self._add_event(event_type="ORDER_SKIPPED", status="SKIPPED",
                        payload={"skip_category": "RISK"})
        self._add_event(event_type="ORDER_SUBMITTED", status="SUBMITTED",
                        payload={"check_name": "fee_guard"})
        # Noise: unrelated events must be filtered out.
        self._add_event(event_type="CONTROL_PAUSE")
        self._add_event(event_type="ORDER_SYNCED")
        self._add_event(event_type="LLM_ANALYSIS")

        checks = RiskTimelineService(self._db()).get_trade_risk_checks()
        event_types = [c["event_type"] for c in checks]
        assert sorted(event_types) == ["ORDER_SKIPPED", "ORDER_SUBMITTED"]

    def test_pass_vs_block_classification(self) -> None:
        self._add_event(event_type="ORDER_SKIPPED",
                        payload={"skip_category": "FEE"})
        self._add_event(event_type="ORDER_SUBMITTED")

        checks = RiskTimelineService(self._db()).get_trade_risk_checks()
        by_type = {c["event_type"]: c for c in checks}
        assert by_type["ORDER_SKIPPED"]["passed"] is False
        assert by_type["ORDER_SUBMITTED"]["passed"] is True

    def test_grouped_by_trade_id(self) -> None:
        # One round trip: skip (FEE) then submit, both under broker_order_id 42.
        self._add_event(
            event_type="ORDER_SKIPPED",
            broker_order_id="42",
            created_at=_NOW,
            payload={"skip_category": "FEE"},
        )
        self._add_event(
            event_type="ORDER_SUBMITTED",
            broker_order_id="42",
            created_at=_NOW + timedelta(seconds=5),
        )
        # A different round trip must be excluded by the trade_id filter.
        self._add_event(
            event_type="ORDER_SKIPPED",
            broker_order_id="99",
            payload={"skip_category": "RISK"},
        )

        checks = RiskTimelineService(self._db()).get_trade_risk_checks(trade_id=42)
        assert len(checks) == 2
        # Chronological order preserved.
        assert checks[0]["event_type"] == "ORDER_SKIPPED"
        assert checks[1]["event_type"] == "ORDER_SUBMITTED"
        assert all(c["trade_id"] == "42" for c in checks)

    def test_symbol_filter_and_limit(self) -> None:
        self._add_event(event_type="ORDER_SKIPPED", symbol="AAPL.US",
                        payload={"skip_category": "RISK"})
        self._add_event(event_type="ORDER_SKIPPED", symbol="MSFT.US",
                        payload={"skip_category": "RISK"})
        self._add_event(event_type="ORDER_SUBMITTED", symbol="AAPL.US")

        aapl = RiskTimelineService(self._db()).get_trade_risk_checks(symbol="AAPL.US")
        assert len(aapl) == 2
        assert all(c["symbol"] == "AAPL.US" for c in aapl)

        limited = RiskTimelineService(self._db()).get_trade_risk_checks(limit=1)
        assert len(limited) == 1

    def test_payload_extraction(self) -> None:
        self._add_event(
            event_type="ORDER_SKIPPED",
            status="SKIPPED",
            payload={
                "skip_category": "FEE",
                "check_name": "fee_guard",
                "reason": "fee-adjusted profit below minimum",
                "threshold": 1.5,
                "actual_value": 0.8,
            },
        )
        checks = RiskTimelineService(self._db()).get_trade_risk_checks()
        c = checks[0]
        assert c["passed"] is False
        assert c["check_name"] == "fee_guard"
        assert c["reason"] == "fee-adjusted profit below minimum"
        assert c["skip_category"] == "FEE"
        assert c["threshold"] == 1.5
        assert c["actual_value"] == 0.8


class TestRiskSummary(_Base):
    def test_category_breakdown(self) -> None:
        # 2 FEE blocks, 1 SESSION block, 1 RISK block, 1 pass.
        self._add_event(event_type="ORDER_SKIPPED", payload={"skip_category": "FEE"})
        self._add_event(event_type="ORDER_SKIPPED", payload={"skip_category": "FEE"})
        self._add_event(event_type="ORDER_SKIPPED", payload={"skip_category": "SESSION"})
        self._add_event(event_type="ORDER_SKIPPED", payload={"skip_category": "RISK"})
        self._add_event(event_type="ORDER_SUBMITTED")

        summary = RiskTimelineService(self._db()).get_risk_summary(hours=24)
        assert summary["total_checks"] == 5
        assert summary["passed"] == 1
        assert summary["blocked"] == 4
        assert summary["by_category"]["FEE"] == 2
        assert summary["by_category"]["SESSION"] == 1
        assert summary["by_category"]["RISK"] == 1
        assert summary["by_category"]["COOLDOWN"] == 0

    def test_recent_blocks_capped_at_ten(self) -> None:
        # Relative chronology is under test (most-recent-first ordering); anchor
        # the base to the current time so all events land inside the trailing
        # 24h summary window regardless of when the suite runs.
        base = _now_utc()
        for i in range(12):
            self._add_event(
                event_type="ORDER_SKIPPED",
                created_at=base - timedelta(minutes=i),
                payload={"skip_category": "RISK"},
            )
        summary = RiskTimelineService(self._db()).get_risk_summary(hours=24)
        assert summary["blocked"] == 12
        assert len(summary["recent_blocks"]) == 10
        # Most recent first.
        assert summary["recent_blocks"][0]["passed"] is False

    def test_window_excludes_old_events(self) -> None:
        recent = datetime.now(timezone.utc) - timedelta(hours=1)
        old = datetime.now(timezone.utc) - timedelta(hours=48)
        self._add_event(event_type="ORDER_SKIPPED", created_at=recent,
                        payload={"skip_category": "FEE"})
        self._add_event(event_type="ORDER_SKIPPED", created_at=old,
                        payload={"skip_category": "RISK"})

        summary = RiskTimelineService(self._db()).get_risk_summary(hours=24)
        assert summary["total_checks"] == 1
        assert summary["by_category"]["FEE"] == 1
        assert summary["by_category"]["RISK"] == 0

    def test_broker_rejection_buckets_to_risk(self) -> None:
        self._add_event(event_type="ORDER_REJECTED", status="REJECTED",
                        payload={"reason": "broker rejected: insufficient buying power"})
        summary = RiskTimelineService(self._db()).get_risk_summary(hours=24)
        assert summary["blocked"] == 1
        # No skip_category → defaults to RISK.
        assert summary["by_category"]["RISK"] == 1


class TestRiskTimelineApi(_Base):
    def test_checks_endpoint_with_filters(self) -> None:
        self._add_event(event_type="ORDER_SKIPPED", symbol="AAPL.US",
                        broker_order_id="7", payload={"skip_category": "RISK"})
        resp = self.client.get(
            "/api/risk-timeline/checks",
            params={"symbol": "AAPL.US", "trade_id": 7},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body) == 1
        assert body[0]["symbol"] == "AAPL.US"
        assert body[0]["trade_id"] == "7"
        assert body[0]["passed"] is False

    def test_summary_endpoint_returns_breakdown(self) -> None:
        self._add_event(event_type="ORDER_SKIPPED", payload={"skip_category": "FEE"})
        self._add_event(event_type="ORDER_SUBMITTED")
        resp = self.client.get("/api/risk-timeline/summary", params={"hours": 24})
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_checks"] == 2
        assert body["passed"] == 1
        assert body["blocked"] == 1
        assert body["by_category"]["FEE"] == 1
        assert len(body["recent_blocks"]) == 1

    def test_limit_validation(self) -> None:
        # limit must be >= 1 (Query ge=1) → 0 is a 422.
        resp = self.client.get("/api/risk-timeline/checks", params={"limit": 0})
        assert resp.status_code == 422
