"""Audit log browse API — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_audit_log_api_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AuditLog, Base
from app.services.audit_log_service import AuditLogService


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
        settings.api_key = ""
        db = Session(bind=self.engine)
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _add(
        self,
        action: str,
        created_at: datetime,
        *,
        severity: str = "INFO",
        actor_hash: str = "abc123",
        source_ip: str = "127.0.0.1",
        summary: dict | None = None,
        result: str = "SUCCESS",
    ) -> int:
        import json

        db = self._db()
        row = AuditLog(
            action=action,
            severity=severity,
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=json.dumps(summary or {}, ensure_ascii=False),
            result=result,
            created_at=created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc),
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        row_id = row.id
        db.close()
        return row_id


class TestAuditLogService(_Base):
    def test_empty_data(self) -> None:
        page = AuditLogService(self._db()).list_logs()
        assert page.items == []
        assert page.total == 0
        assert page.limit == 100
        assert page.offset == 0

    def test_newest_first_order(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add("ACTION_B", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))
        page = AuditLogService(self._db()).list_logs()
        assert page.total == 2
        assert [i.action for i in page.items] == ["ACTION_B", "ACTION_A"]

    def test_action_and_severity_filters(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc), severity="INFO")
        self._add("ACTION_B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc), severity="CRITICAL")
        page = AuditLogService(self._db()).list_logs(action="action_b")
        assert page.total == 1
        assert page.items[0].action == "ACTION_B"
        page = AuditLogService(self._db()).list_logs(severity="critical")
        assert page.total == 1
        assert page.items[0].severity == "CRITICAL"
        page = AuditLogService(self._db()).list_logs(action="MISSING")
        assert page.total == 0

    def test_date_range_filter(self) -> None:
        self._add("A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add("B", datetime(2026, 6, 16, 23, 59, 0, tzinfo=timezone.utc))
        page = AuditLogService(self._db()).list_logs(
            from_date=datetime(2026, 6, 15).date(),
            to_date=datetime(2026, 6, 16).date(),
        )
        assert page.total == 1
        assert page.items[0].action == "B"
        # to_date is inclusive of that day
        page = AuditLogService(self._db()).list_logs(to_date=datetime(2026, 6, 16).date())
        assert page.total == 2

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            AuditLogService(self._db()).list_logs(
                from_date=datetime(2026, 6, 17).date(),
                to_date=datetime(2026, 6, 16).date(),
            )

    def test_bounded_pagination(self) -> None:
        for day in range(1, 6):
            self._add(f"DAY_{day}", datetime(2026, 6, day, 10, 0, 0, tzinfo=timezone.utc))
        page = AuditLogService(self._db()).list_logs(limit=2, offset=0)
        assert page.total == 5
        assert len(page.items) == 2
        assert [i.action for i in page.items] == ["DAY_5", "DAY_4"]
        page = AuditLogService(self._db()).list_logs(limit=2, offset=4)
        assert len(page.items) == 1
        assert page.items[0].action == "DAY_1"
        # out-of-range offset -> empty page, total intact
        page = AuditLogService(self._db()).list_logs(limit=2, offset=100)
        assert page.items == []
        assert page.total == 5

    def test_limit_is_bounded(self) -> None:
        for day in range(1, 6):
            self._add(f"DAY_{day}", datetime(2026, 6, day, 10, 0, 0, tzinfo=timezone.utc))
        page = AuditLogService(self._db()).list_logs(limit=10_000)
        assert page.limit == 500
        assert len(page.items) == 5

    def test_request_summary_parsed_dict(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc), summary={"symbol": "AAPL.US"})
        page = AuditLogService(self._db()).list_logs()
        assert page.items[0].request_summary == {"symbol": "AAPL.US"}

    def test_non_json_summary_degrades_to_raw(self) -> None:
        db = self._db()
        db.add(AuditLog(
            action="ACTION_X",
            severity="INFO",
            actor_hash="abc",
            source_ip="",
            request_summary="not-json-text",
            result="SUCCESS",
        ))
        db.commit()
        db.close()
        page = AuditLogService(self._db()).list_logs()
        assert page.items[0].request_summary == {"raw": "not-json-text"}


class TestAuditLogAPI(_Base):
    def test_endpoint_empty(self) -> None:
        resp = self.client.get("/api/audit-logs")
        assert resp.status_code == 200, resp.text
        assert resp.json() == {"items": [], "total": 0, "limit": 100, "offset": 0}

    def test_endpoint_rows_and_fields(self) -> None:
        self._add(
            "AUDIT_PACK_EXPORT",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            severity="INFO",
            actor_hash="deadbeef",
            source_ip="127.0.0.1",
            summary={"symbol": "AAPL.US"},
            result="SUCCESS",
        )
        resp = self.client.get("/api/audit-logs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["action"] == "AUDIT_PACK_EXPORT"
        assert item["severity"] == "INFO"
        assert item["actor_hash"] == "deadbeef"
        assert item["source_ip"] == "127.0.0.1"
        assert item["result"] == "SUCCESS"
        assert item["request_summary"] == {"symbol": "AAPL.US"}

    def test_endpoint_filters_and_pagination(self) -> None:
        for day in range(1, 5):
            self._add(f"ACTION_{day}", datetime(2026, 6, day, 10, 0, 0, tzinfo=timezone.utc))
        resp = self.client.get("/api/audit-logs", params={"limit": 2, "offset": 0})
        assert resp.status_code == 200
        assert resp.json()["total"] == 4
        assert [i["action"] for i in resp.json()["items"]] == ["ACTION_4", "ACTION_3"]
        resp = self.client.get("/api/audit-logs", params={"action": "action_2"})
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["action"] == "ACTION_2"

    def test_endpoint_invalid_date_range_422(self) -> None:
        resp = self.client.get(
            "/api/audit-logs",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422

    def test_endpoint_invalid_date_format_422(self) -> None:
        resp = self.client.get("/api/audit-logs", params={"from_date": "not-a-date"})
        assert resp.status_code == 422

    def test_auth_enforced_when_api_key_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "audit-secret")
        assert self.client.get("/api/audit-logs").status_code == 401
        resp = self.client.get("/api/audit-logs", headers={"X-API-Key": "audit-secret"})
        assert resp.status_code == 200

    def test_auth_disabled_in_dev_without_key(self) -> None:
        settings.api_key = ""
        resp = self.client.get("/api/audit-logs")
        assert resp.status_code == 200
