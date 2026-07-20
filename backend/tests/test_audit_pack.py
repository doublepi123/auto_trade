"""Read-only incident audit pack export API."""
from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api.deps import get_audit_logger
from app.config import settings
from app.database import get_db
from app.main import app
from app.models import (
    AuditLog,
    Base,
    OrderRecord,
    RiskEvent,
    RuntimeStateSnapshot,
    StrategyConfig,
    TradeEvent,
)


TEST_DATABASE_URL = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_audit_pack_{os.getpid()}.db"
)
SECRET_VALUE = "audit-pack-secret-sct-value"


class _AuditSpy:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict[str, object]]] = []

    def record(self, action: str, **details: object) -> None:
        self.records.append((action, details))


class TestAuditPackAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            TEST_DATABASE_URL,
            connect_args={"check_same_thread": False},
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)
        cls.session_factory = sessionmaker(bind=cls.engine)
        cls.audit = _AuditSpy()

        def override_get_db():
            with cls.session_factory() as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_audit_logger] = lambda: cls.audit
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        cls.client.close()
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_audit_logger, None)
        Base.metadata.drop_all(bind=cls.engine)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        self.audit.records.clear()
        with self.session_factory() as db:
            for model in (
                TradeEvent,
                RiskEvent,
                AuditLog,
                RuntimeStateSnapshot,
                OrderRecord,
                StrategyConfig,
            ):
                db.query(model).delete()
            db.commit()

    def teardown_method(self) -> None:
        settings.api_key = ""

    def _seed_config(self, *, secret: str = SECRET_VALUE) -> None:
        with self.session_factory() as db:
            db.add(
                StrategyConfig(
                    symbol="AAPL.US",
                    market="US",
                    buy_low=100.0,
                    sell_high=120.0,
                    sct_key=secret,
                )
            )
            db.commit()

    def _seed_sections(self) -> None:
        included = datetime(2026, 7, 20, 12, tzinfo=timezone.utc)
        excluded = datetime(2026, 7, 19, 12, tzinfo=timezone.utc)
        with self.session_factory() as db:
            db.add_all(
                [
                    OrderRecord(
                        broker_order_id="aapl-in",
                        symbol="AAPL.US",
                        side="BUY",
                        quantity=2,
                        price=101.0,
                        status="FILLED",
                        created_at=included,
                        filled_at=included,
                    ),
                    OrderRecord(
                        broker_order_id="msft-in",
                        symbol="MSFT.US",
                        side="BUY",
                        quantity=1,
                        price=201.0,
                        status="FILLED",
                        created_at=included,
                        filled_at=included,
                    ),
                    OrderRecord(
                        broker_order_id="aapl-old",
                        symbol="AAPL.US",
                        side="SELL",
                        quantity=2,
                        price=99.0,
                        status="FILLED",
                        created_at=excluded,
                        filled_at=excluded,
                    ),
                    TradeEvent(
                        event_type="ORDER_FILLED",
                        symbol="AAPL.US",
                        broker_order_id="aapl-in",
                        status="FILLED",
                        message="included trade event",
                        created_at=included,
                    ),
                    TradeEvent(
                        event_type="ORDER_FILLED",
                        symbol="MSFT.US",
                        message="other symbol",
                        created_at=included,
                    ),
                    TradeEvent(
                        event_type="ORDER_FILLED",
                        symbol="AAPL.US",
                        message="old event",
                        created_at=excluded,
                    ),
                    RiskEvent(
                        event_type="DAILY_LOSS",
                        reason="AAPL.US daily loss threshold",
                        created_at=included,
                    ),
                    RiskEvent(
                        event_type="DAILY_LOSS",
                        reason="MSFT.US daily loss threshold",
                        created_at=included,
                    ),
                    RiskEvent(
                        event_type="DAILY_LOSS",
                        reason="AAPL.US old risk event",
                        created_at=excluded,
                    ),
                    RuntimeStateSnapshot(
                        symbol="AAPL.US",
                        engine_state="flat",
                        daily_pnl=-25.0,
                        created_at=included,
                    ),
                    RuntimeStateSnapshot(
                        symbol="MSFT.US",
                        engine_state="flat",
                        daily_pnl=10.0,
                        created_at=included,
                    ),
                    RuntimeStateSnapshot(
                        symbol="AAPL.US",
                        engine_state="long",
                        daily_pnl=-5.0,
                        created_at=excluded,
                    ),
                ]
            )
            db.commit()

    def test_bundle_structure_counts_and_attachment(self) -> None:
        self._seed_config()
        self._seed_sections()

        response = self.client.get(
            "/api/audit-pack/export",
            params={"from_date": "2026-07-20", "to_date": "2026-07-20"},
        )

        assert response.status_code == 200, response.text
        assert response.headers["content-type"].startswith("application/json")
        body = response.json()
        generated_date = datetime.fromisoformat(body["generated_at"]).strftime("%Y%m%d")
        assert response.headers["content-disposition"] == (
            f'attachment; filename="audit_pack_AAPL_US_{generated_date}.json"'
        )
        assert set(body) == {
            "generated_at",
            "symbol",
            "from_date",
            "to_date",
            "strategy_config",
            "orders",
            "trade_events",
            "risk_events",
            "runtime_snapshots",
        }
        assert body["symbol"] == "AAPL.US"
        assert body["strategy_config"]["buy_low"] == 100.0
        assert len(body["orders"]) == 1
        assert len(body["trade_events"]) == 1
        assert len(body["risk_events"]) == 1
        assert len(body["runtime_snapshots"]) == 1

    def test_symbol_and_date_filters_apply_to_every_section(self) -> None:
        self._seed_config()
        self._seed_sections()

        response = self.client.get(
            "/api/audit-pack/export",
            params={
                "symbol": "msft.us",
                "from_date": "2026-07-20",
                "to_date": "2026-07-20",
            },
        )

        body = response.json()
        assert response.status_code == 200, response.text
        assert [row["symbol"] for row in body["orders"]] == ["MSFT.US"]
        assert [row["symbol"] for row in body["trade_events"]] == ["MSFT.US"]
        assert [row["event_type"] for row in body["risk_events"]] == ["DAILY_LOSS"]
        assert [row["daily_pnl"] for row in body["runtime_snapshots"]] == [10.0]

    def test_strategy_secret_is_excluded_from_entire_serialized_bundle(self) -> None:
        self._seed_config()

        response = self.client.get("/api/audit-pack/export")

        assert response.status_code == 200, response.text
        assert SECRET_VALUE not in response.text
        assert "sct_key" not in response.text
        assert "longbridge_access_token" not in response.text
        assert "notification_channels" not in response.text

    def test_empty_database_returns_valid_empty_bundle(self) -> None:
        response = self.client.get("/api/audit-pack/export")

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["symbol"] == ""
        assert body["orders"] == []
        assert body["trade_events"] == []
        assert body["risk_events"] == []
        assert body["runtime_snapshots"] == []

    def test_invalid_date_returns_422(self) -> None:
        response = self.client.get(
            "/api/audit-pack/export",
            params={"from_date": "not-a-date"},
        )

        assert response.status_code == 422

    def test_export_writes_one_summary_audit_record_without_payload(self) -> None:
        self._seed_config()
        self._seed_sections()

        response = self.client.get("/api/audit-pack/export", headers={"X-API-Key": "actor"})

        assert response.status_code == 200, response.text
        assert len(self.audit.records) == 1
        action, details = self.audit.records[0]
        assert action == "AUDIT_PACK_EXPORT"
        assert details["result"] == "SUCCESS"
        assert details["request_summary"] == {
            "symbol": "AAPL.US",
            "orders": 2,
            "trade_events": 2,
            "risk_events": 2,
            "runtime_snapshots": 2,
        }
        assert SECRET_VALUE not in json.dumps(details)

    def test_auth_is_enforced_when_api_key_is_configured(self) -> None:
        self._seed_config()
        settings.api_key = "audit-pack-key"

        assert self.client.get("/api/audit-pack/export").status_code == 401
        response = self.client.get(
            "/api/audit-pack/export",
            headers={"X-API-Key": "audit-pack-key"},
        )

        assert response.status_code == 200
