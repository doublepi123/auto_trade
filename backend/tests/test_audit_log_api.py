"""Audit log browse API — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

import pytest

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_audit_log_api_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import AuditLog, Base
from app.schemas import AuditLogStatsResponse
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


class TestAuditLogStats(_Base):
    def _stats(self, **kwargs):
        return AuditLogService(self._db()).stats(**kwargs)

    def test_empty_data(self) -> None:
        stats = self._stats()
        assert stats.total == 0
        assert stats.by_action == []
        assert stats.by_severity == []
        assert stats.by_actor == []
        assert stats.by_day == []
        assert stats.action_other_total == 0
        assert stats.severity_other_total == 0
        assert stats.actor_other_total == 0

    def test_total_and_categorical_aggregation(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc), severity="INFO", actor_hash="actor1")
        self._add("ACTION_A", datetime(2026, 6, 14, 11, 0, 0, tzinfo=timezone.utc), severity="INFO", actor_hash="actor1")
        self._add("ACTION_B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc), severity="CRITICAL", actor_hash="actor2")
        stats = self._stats()
        assert stats.total == 3
        # count desc then key asc; ACTION_A (2) before ACTION_B (1)
        assert [(b.key, b.count) for b in stats.by_action] == [
            ("ACTION_A", 2),
            ("ACTION_B", 1),
        ]
        assert [(b.key, b.count) for b in stats.by_severity] == [
            ("INFO", 2),
            ("CRITICAL", 1),
        ]
        assert [(b.actor_hash, b.count) for b in stats.by_actor] == [
            ("actor1", 2),
            ("actor2", 1),
        ]

    def test_count_conservation_categorical(self) -> None:
        for i in range(10):
            self._add(f"A_{i}", datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc))
        stats = self._stats()
        assert stats.total == 10
        assert sum(b.count for b in stats.by_action) + stats.action_other_total == 10
        assert sum(b.count for b in stats.by_severity) + stats.severity_other_total == 10
        assert sum(b.count for b in stats.by_actor) + stats.actor_other_total == 10

    def test_daily_rows_chronological_and_conserved(self) -> None:
        self._add("A", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))
        self._add("B", datetime(2026, 6, 14, 23, 0, 0, tzinfo=timezone.utc))
        self._add("C", datetime(2026, 6, 15, 0, 30, 0, tzinfo=timezone.utc))
        stats = self._stats()
        days = [(b.day.isoformat(), b.count) for b in stats.by_day]
        assert days == [("2026-06-14", 1), ("2026-06-15", 1), ("2026-06-16", 1)]
        assert sum(b.count for b in stats.by_day) == stats.total

    def test_utc_day_boundary(self) -> None:
        # 2026-06-14 23:59 UTC and 2026-06-15 00:00 UTC are different days
        self._add("LATE", datetime(2026, 6, 14, 23, 59, 0, tzinfo=timezone.utc))
        self._add("EARLY", datetime(2026, 6, 15, 0, 1, 0, tzinfo=timezone.utc))
        stats = self._stats()
        days = {b.day.isoformat(): b.count for b in stats.by_day}
        assert days == {"2026-06-14": 1, "2026-06-15": 1}

    def test_deterministic_tie_order_key_asc(self) -> None:
        # Same count -> key ascending
        for letter in ("ZETA", "ALPHA", "MIKE"):
            self._add(letter, datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        stats = self._stats()
        assert [b.key for b in stats.by_action] == ["ALPHA", "MIKE", "ZETA"]

    def test_action_filter_reuses_list_semantics(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add("ACTION_B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc))
        stats = self._stats(action="action_b")
        assert stats.total == 1
        assert [(b.key, b.count) for b in stats.by_action] == [("ACTION_B", 1)]
        assert stats.filters == {"action": "ACTION_B"}

    def test_severity_filter_case_insensitive(self) -> None:
        self._add("A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc), severity="INFO")
        self._add("B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc), severity="CRITICAL")
        stats = self._stats(severity="critical")
        assert stats.total == 1
        assert stats.by_severity[0].key == "CRITICAL"

    def test_date_range_filter_inclusive(self) -> None:
        self._add("A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add("B", datetime(2026, 6, 16, 23, 59, 0, tzinfo=timezone.utc))
        stats = self._stats(
            from_date=datetime(2026, 6, 15).date(),
            to_date=datetime(2026, 6, 16).date(),
        )
        assert stats.total == 1
        assert stats.by_action[0].key == "B"
        # to_date is inclusive of that day
        stats = self._stats(to_date=datetime(2026, 6, 16).date())
        assert stats.total == 2

    def test_invalid_date_range_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            self._stats(
                from_date=datetime(2026, 6, 17).date(),
                to_date=datetime(2026, 6, 16).date(),
            )

    def test_categorical_truncation_reports_other_total(self) -> None:
        # Exceed the action bucket cap (50) to force truthful truncation.
        for i in range(60):
            self._add(f"ACTION_{i:02d}", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        stats = self._stats()
        assert stats.total == 60
        assert len(stats.by_action) == 50
        kept_total = sum(b.count for b in stats.by_action)
        assert kept_total + stats.action_other_total == 60
        assert stats.action_other_total == 10

    def test_pseudonymous_actor_only_no_raw_material(self) -> None:
        self._add(
            "ACTION_A",
            datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            actor_hash="deadbeefcafe",
            source_ip="10.0.0.1",
            summary={"secret": "shh", "api_key": "k"},
        )
        stats = self._stats()
        payload = stats.model_dump(mode="json")
        # Only the pseudonymous actor_hash is exposed; no IP, raw key, or body.
        assert stats.by_actor[0].actor_hash == "deadbeefcafe"
        dumped = str(payload)
        assert "10.0.0.1" not in dumped
        assert "shh" not in dumped
        assert "source_ip" not in dumped
        assert "request_summary" not in dumped


class TestAuditLogStatsAPI(_Base):
    def test_endpoint_empty(self) -> None:
        resp = self.client.get("/api/audit-logs/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 0
        assert data["by_action"] == []
        assert data["by_day"] == []
        assert data["action_other_total"] == 0

    def test_endpoint_aggregates_and_conserves(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc), severity="INFO")
        self._add("ACTION_B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc), severity="CRITICAL")
        resp = self.client.get("/api/audit-logs/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 2
        # Validate the response shape round-trips through the schema.
        validated = AuditLogStatsResponse.model_validate(data)
        assert validated.total == 2
        assert sum(b.count for b in validated.by_action) + validated.action_other_total == 2
        assert sum(b.count for b in validated.by_day) == 2

    def test_endpoint_filters(self) -> None:
        self._add("ACTION_A", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add("ACTION_B", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc))
        resp = self.client.get("/api/audit-logs/stats", params={"action": "action_a"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 1

    def test_endpoint_invalid_date_range_422(self) -> None:
        resp = self.client.get(
            "/api/audit-logs/stats",
            params={"from_date": "2026-06-17", "to_date": "2026-06-16"},
        )
        assert resp.status_code == 422

    def test_endpoint_invalid_date_format_422(self) -> None:
        resp = self.client.get("/api/audit-logs/stats", params={"from_date": "not-a-date"})
        assert resp.status_code == 422


class TestAuditLogStatsSnapshotConservation:
    """The stats snapshot must conserve total/buckets/overflow across a
    concurrent commit (finding D5).

    Uses a file-backed SQLite DB so a second session can INSERT/COMMIT between
    the aggregate queries. The snapshot must observe ONE consistent committed
    state, so total == sum(by_action) + action_other_total both before and
    after a concurrent insert, and the snapshot taken mid-flight never reflects
    a half-committed population.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_audit_stats_snapshot_{os.getpid()}.db",
        )
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _add(self, db, action, severity="INFO", actor_hash="a1"):
        db.add(
            AuditLog(
                action=action,
                severity=severity,
                actor_hash=actor_hash,
                source_ip="",
                request_summary="{}",
                result="SUCCESS",
            )
        )
        db.commit()

    def test_snapshot_conserves_total_and_overflow(self) -> None:
        # Seed enough distinct actions to force overflow under the cap.
        db = Session(bind=self.engine)
        for i in range(60):
            self._add(db, f"ACTION_{i:02d}", actor_hash=f"actor_{i % 5}")
        db.close()

        service_db = Session(bind=self.engine)
        stats = AuditLogService(service_db).stats()
        service_db.close()

        assert stats.total == 60
        # Conservation: kept + overflow == total for every categorical dim.
        assert (
            sum(b.count for b in stats.by_action) + stats.action_other_total
            == stats.total
        )
        assert (
            sum(b.count for b in stats.by_severity)
            + stats.severity_other_total
            == stats.total
        )
        assert (
            sum(b.count for b in stats.by_actor) + stats.actor_other_total
            == stats.total
        )
        # Daily buckets always sum exactly to total.
        assert sum(b.count for b in stats.by_day) == stats.total

    def test_concurrent_commit_does_not_split_snapshot(self) -> None:
        # Seed an initial population.
        db = Session(bind=self.engine)
        for i in range(10):
            self._add(db, f"SEED_{i:02d}", actor_hash="seed_actor")
        db.close()

        # Take a snapshot on a dedicated read session.
        reader = Session(bind=self.engine)
        stats_before = AuditLogService(reader).stats()
        # While the reader holds its snapshot, a writer commits new rows on a
        # separate session.
        writer = Session(bind=self.engine)
        for i in range(5):
            self._add(writer, "CONCURRENT", actor_hash="concurrent_actor")
        writer.close()

        stats_after = AuditLogService(reader).stats()
        reader.close()

        # Both snapshots were taken over consistent committed state. The
        # concurrent commit must not have split either snapshot: conservation
        # holds for both, and the "before" snapshot does not see the 5 new rows.
        assert stats_before.total == 10
        assert (
            sum(b.count for b in stats_before.by_action)
            + stats_before.action_other_total
            == stats_before.total
        )
        assert (
            sum(b.count for b in stats_after.by_action)
            + stats_after.action_other_total
            == stats_after.total
        )
        # The reader's snapshot was established before the writer committed, so
        # stats_before.total reflects only the seeded 10 rows (SQLite WAL keeps
        # the read snapshot stable for the duration of the read transaction).
        assert stats_before.total == 10

    def test_stats_does_not_mutate_caller_session(self) -> None:
        db = Session(bind=self.engine)
        self._add(db, "ACTION_X")
        # Snapshot the session identity-map / state before stats.
        new_before = len(db.new)
        dirty_before = len(db.dirty)
        deleted_before = len(db.deleted)
        AuditLogService(db).stats()
        # The caller's session is untouched (no staged writes, no flush of
        # pending state).
        assert len(db.new) == new_before
        assert len(db.dirty) == dirty_before
        assert len(db.deleted) == deleted_before
        db.close()


class TestAuditLogStatsCallerConnectionSafety:
    """``stats()`` must never touch the caller's connection/transaction.

    Finding B: when ``Session.get_bind()`` returns a SQLAlchemy ``Connection``
    (a session bound directly to a connection inside an active transaction),
    or the session already has an active transaction, ``stats()`` must FAIL
    SAFELY with ``SnapshotUnavailable`` BEFORE any transaction command — it must
    never begin/commit/rollback or otherwise alter the caller Connection. The
    caller transaction remains active and the sentinel remains rollback-able.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_audit_stats_conn_{os.getpid()}.db",
        )
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )
        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def test_stats_rejects_connection_bound_session_safely(self) -> None:
        # Open a caller connection and begin an explicit transaction.
        caller_connection = self.engine.connect()
        caller_trans = caller_connection.begin()
        try:
            # Bind a session directly to the caller connection and stage an
            # uncommitted sentinel row.
            db = Session(bind=caller_connection)
            db.add(
                AuditLog(
                    action="SENTINEL",
                    severity="INFO",
                    actor_hash="sentinel_actor",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                )
            )
            db.flush()  # stage within the caller transaction, do not commit

            # stats() must reject safely BEFORE touching the caller connection.
            from app.services.snapshot_helper import SnapshotUnavailable

            with pytest.raises(SnapshotUnavailable):
                AuditLogService(db).stats()

            # The caller transaction is still active and the sentinel is still
            # present/rollback-able exactly as before.
            assert caller_trans.is_active
            in_tx = db.query(AuditLog).filter(
                AuditLog.action == "SENTINEL"
            ).count()
            assert in_tx == 1

            # Rolling back the caller transaction removes the sentinel.
            db.close()
            caller_trans.rollback()
            after_rollback = Session(bind=self.engine)
            assert (
                after_rollback.query(AuditLog)
                .filter(AuditLog.action == "SENTINEL")
                .count()
                == 0
            )
            after_rollback.close()
        finally:
            caller_connection.close()

    def test_stats_rejects_active_transaction_session_safely(self) -> None:
        # A session with an active transaction (autoflush after add) must be
        # rejected because a second connection could alias it under single-slot
        # pools.
        db = Session(bind=self.engine)
        db.add(AuditLog(action="X", severity="INFO", actor_hash="a",
                        source_ip="", request_summary="{}", result="SUCCESS"))
        db.flush()  # starts a transaction on the session's connection
        try:
            from app.services.snapshot_helper import SnapshotUnavailable

            assert db.in_transaction()
            with pytest.raises(SnapshotUnavailable):
                AuditLogService(db).stats()
        finally:
            db.rollback()
            db.close()


class TestAuditLogStatsInFlightSnapshot:
    """A real in-flight concurrent snapshot test (finding 3).

    A second connection commits audit rows AFTER the stats total query
    establishes the read snapshot but BEFORE categorical/actor/day queries
    complete in the SAME ``stats()`` call. The response must conserve the
    original snapshot total across action/severity/actor/day/overflow despite
    the writer commit, while a subsequent independent stats call sees the new
    rows.
    """

    @classmethod
    def setup_class(cls) -> None:
        cls.db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_audit_stats_inflight_{os.getpid()}.db",
        )
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)
        # Production-equivalent WAL/busy pragmas via the connect event.
        cls.engine = create_engine(
            f"sqlite:///{cls.db_path}",
            connect_args={"check_same_thread": False},
        )

        @event.listens_for(cls.engine, "connect")
        def _set_pragmas(dbapi_connection, _record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

        Base.metadata.create_all(bind=cls.engine)

    @classmethod
    def teardown_class(cls) -> None:
        cls.engine.dispose()
        if os.path.exists(cls.db_path):
            os.unlink(cls.db_path)

    def setup_method(self) -> None:
        db = Session(bind=self.engine)
        db.query(AuditLog).delete()
        db.commit()
        db.close()

    def _seed(self, count: int) -> None:
        db = Session(bind=self.engine)
        for i in range(count):
            db.add(
                AuditLog(
                    action=f"SEED_{i:02d}",
                    severity="INFO",
                    actor_hash="seed_actor",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                )
            )
        db.commit()
        db.close()

    def test_in_flight_writer_commit_does_not_split_snapshot(self) -> None:
        # Seed 10 rows on the committed state.
        self._seed(10)

        reader = Session(bind=self.engine)
        service = AuditLogService(reader)

        # Coordinate deterministically: the writer commits AFTER the total
        # query establishes the snapshot but BEFORE categorical queries run,
        # by monkeypatching the naturally factored _after_total_query hook.
        writer = Session(bind=self.engine)
        writer_committed = []

        def _commit_writer_during_snapshot() -> None:
            for i in range(5):
                writer.add(
                    AuditLog(
                        action="CONCURRENT",
                        severity="WARNING",
                        actor_hash="concurrent_actor",
                        source_ip="",
                        request_summary="{}",
                        result="SUCCESS",
                    )
                )
            writer.commit()
            writer_committed.append(True)

        service._after_total_query = _commit_writer_during_snapshot  # type: ignore[method-assign]

        try:
            stats = service.stats()
            # The snapshot was established before the writer committed, so the
            # response conserves the original 10-row state across every
            # aggregate despite the in-flight commit.
            assert stats.total == 10
            assert writer_committed == [True]  # writer did commit mid-call
            # Conservation holds against the snapshot total.
            assert (
                sum(b.count for b in stats.by_action)
                + stats.action_other_total
                == stats.total
            )
            assert (
                sum(b.count for b in stats.by_severity)
                + stats.severity_other_total
                == stats.total
            )
            assert (
                sum(b.count for b in stats.by_actor)
                + stats.actor_other_total
                == stats.total
            )
            assert sum(b.count for b in stats.by_day) == stats.total
            # The concurrent rows are NOT visible in this snapshot.
            assert all(b.key != "CONCURRENT" for b in stats.by_action)
            assert all(b.actor_hash != "concurrent_actor" for b in stats.by_actor)

            # A subsequent independent stats call sees the new committed rows.
            stats_after = AuditLogService(reader).stats()
            assert stats_after.total == 15
            assert any(b.key == "CONCURRENT" for b in stats_after.by_action)
        finally:
            writer.close()
            reader.close()


class TestAuditLogStatsPoolIsolation:
    """Finding B: guaranteed caller isolation under aliased-pool scenarios.

    Under StaticPool / SingletonThreadPool / single-slot pools, a second
    ``engine.connect()`` can return the SAME underlying DBAPI connection as the
    caller. ``stats()`` must detect connection-bound / active-transaction
    sessions and fail safely with ``SnapshotUnavailable`` BEFORE any transaction
    command, leaving the caller transaction active and the sentinel rollback-able.
    """

    def _assert_rejects_with_active_sentinel(self, engine) -> None:
        from app.services.snapshot_helper import SnapshotUnavailable

        caller_connection = engine.connect()
        caller_trans = caller_connection.begin()
        try:
            db = Session(bind=caller_connection)
            db.add(
                AuditLog(
                    action="SENTINEL",
                    severity="INFO",
                    actor_hash="sentinel",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                )
            )
            db.flush()
            with pytest.raises(SnapshotUnavailable):
                AuditLogService(db).stats()
            # Caller transaction still active; sentinel still rollback-able.
            assert caller_trans.is_active
            assert (
                db.query(AuditLog).filter(AuditLog.action == "SENTINEL").count()
                == 1
            )
            db.close()
            caller_trans.rollback()
        finally:
            caller_connection.close()

    def test_static_pool_connection_bound_rejected(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            Base.metadata.create_all(engine)
            self._assert_rejects_with_active_sentinel(engine)
        finally:
            engine.dispose()

    def test_singleton_thread_pool_connection_bound_rejected(self) -> None:
        from sqlalchemy.pool import SingletonThreadPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=SingletonThreadPool,
        )
        try:
            Base.metadata.create_all(engine)
            self._assert_rejects_with_active_sentinel(engine)
        finally:
            engine.dispose()

    def test_normal_pooled_file_sqlite_succeeds(self) -> None:
        # The ordinary production-like path (file SQLite, default QueuePool,
        # Engine-bound session, no active transaction) must still succeed.
        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_audit_stats_normal_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        try:
            Base.metadata.create_all(engine)
            db = Session(bind=engine)
            db.add(AuditLog(action="OK", severity="INFO", actor_hash="a",
                            source_ip="", request_summary="{}", result="SUCCESS"))
            db.commit()
            db.close()
            reader = Session(bind=engine)
            stats = AuditLogService(reader).stats()
            reader.close()
            assert stats.total == 1
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestAuditLogStatsAPI503:
    """Finding B.5: SnapshotUnavailable maps to HTTP 503 through the route.

    Uses a local FastAPI app with a connection-bound session dependency so the
    global ``app`` dependency overrides are not disturbed.
    """

    def test_stats_endpoint_returns_503_for_connection_bound_session(
        self,
    ) -> None:
        from fastapi import FastAPI

        from app.api.audit_log import router as audit_log_router

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_audit_stats_503_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=engine)
        try:
            local_app = FastAPI()
            local_app.include_router(audit_log_router)

            def override_get_db():
                # A connection-bound session dependency (the unsafe case).
                conn = engine.connect()
                try:
                    yield Session(bind=conn)
                finally:
                    conn.close()

            local_app.dependency_overrides[get_db] = override_get_db
            client = TestClient(local_app)
            resp = client.get("/api/audit-logs/stats")
            assert resp.status_code == 503, resp.text
            assert "snapshot unavailable" in resp.json()["detail"].lower()
            client.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)


class TestSnapshotHelperPoolRejection:
    """Finding 1: pool modes that cannot guarantee a distinct physical
    connection are rejected BEFORE ``engine.connect()`` / any transaction
    command, even with a FRESH Engine-bound reader Session.

    Under StaticPool / SingletonThreadPool, a second ``engine.connect()``
    returns the SAME underlying DBAPI connection as an active owner. The helper
    must reject unconditionally, preserving the owner's uncommitted sentinel
    and active transaction.
    """

    def _assert_fresh_reader_rejected_with_active_owner(
        self,
        engine,
        *,
        pool_name: str,
    ) -> None:
        from app.services.snapshot_helper import (
            SnapshotUnavailable,
            open_read_snapshot,
        )

        # An active owner holds the single connection with an uncommitted
        # sentinel.
        owner_conn = engine.connect()
        owner_trans = owner_conn.begin()
        try:
            owner_session = Session(bind=owner_conn)
            owner_session.add(
                AuditLog(
                    action="OWNER_SENTINEL",
                    severity="INFO",
                    actor_hash="owner",
                    source_ip="",
                    request_summary="{}",
                    result="SUCCESS",
                )
            )
            owner_session.flush()

            # A FRESH Engine-bound reader Session (not connection-bound) with no
            # active transaction. Under StaticPool/SingletonThreadPool this
            # would still alias the owner's connection, so it must be rejected.
            reader = Session(bind=engine)
            try:
                with pytest.raises(SnapshotUnavailable) as exc_info:
                    open_read_snapshot(reader, lambda conn: None)
                assert pool_name in str(exc_info.value).lower() or (
                    "pool" in str(exc_info.value).lower()
                )
            finally:
                reader.close()

            # The owner's transaction is still active and the sentinel is still
            # present/rollback-able exactly as before.
            assert owner_trans.is_active
            assert (
                owner_session.query(AuditLog)
                .filter(AuditLog.action == "OWNER_SENTINEL")
                .count()
                == 1
            )
            owner_session.close()
            owner_trans.rollback()
        finally:
            owner_conn.close()

    def test_static_pool_fresh_reader_rejected_with_active_owner(self) -> None:
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        try:
            Base.metadata.create_all(engine)
            self._assert_fresh_reader_rejected_with_active_owner(
                engine, pool_name="staticpool"
            )
        finally:
            engine.dispose()

    def test_singleton_thread_pool_fresh_reader_rejected_with_active_owner(
        self,
    ) -> None:
        from sqlalchemy.pool import SingletonThreadPool

        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=SingletonThreadPool,
        )
        try:
            Base.metadata.create_all(engine)
            self._assert_fresh_reader_rejected_with_active_owner(
                engine, pool_name="singletonthreadpool"
            )
        finally:
            engine.dispose()

    def test_exhausted_queue_pool_rejected_without_blocking(self) -> None:
        # A single-slot QueuePool (size=1, overflow=0) with the one connection
        # checked out must be rejected via public pool state without waiting.
        from sqlalchemy.pool import QueuePool

        from app.services.snapshot_helper import (
            SnapshotUnavailable,
            open_read_snapshot,
        )

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_exhausted_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
        )
        try:
            Base.metadata.create_all(engine)
            # Check out the single connection.
            owner_conn = engine.connect()
            try:
                reader = Session(bind=engine)
                with pytest.raises(SnapshotUnavailable) as exc_info:
                    open_read_snapshot(reader, lambda conn: None)
                assert "exhausted" in str(exc_info.value).lower()
                reader.close()
            finally:
                owner_conn.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_begin_failure_raises_and_no_query_proceeds(self, monkeypatch) -> None:
        # If the explicit BEGIN fails, no query must proceed and the owned
        # connection must be closed/released.
        from app.services.snapshot_helper import (
            SnapshotUnavailable,
            open_read_snapshot,
        )

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_begin_fail_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        try:
            Base.metadata.create_all(engine)
            reader = Session(bind=engine)
            query_ran = []

            def _query(conn):
                query_ran.append(True)
                return "result"

            # Monkeypatch the DBAPI connection's execute to fail on BEGIN only.
            real_connect = engine.connect

            class _BadBeginConnection:
                def __init__(self, real):
                    self._real = real

                @property
                def connection(self):
                    real_driver = self._real.connection

                    class _Driver:
                        def execute(self, stmt, *args):
                            if isinstance(stmt, str) and stmt.upper() == "BEGIN":
                                raise RuntimeError("injected BEGIN failure")
                            return real_driver.execute(stmt, *args)

                        def rollback(self):
                            return real_driver.rollback()

                    return _Driver()

                def close(self):
                    return self._real.close()

            def _fake_connect():
                return _BadBeginConnection(real_connect())

            monkeypatch.setattr(engine, "connect", _fake_connect)

            with pytest.raises(SnapshotUnavailable):
                open_read_snapshot(reader, _query)
            assert query_ran == []  # no query proceeded
            reader.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_normal_pooled_file_sqlite_succeeds_via_helper(self) -> None:
        # The ordinary production-like path (file SQLite, default QueuePool,
        # Engine-bound session, no active transaction) must still succeed.
        from app.services.snapshot_helper import open_read_snapshot

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_normal_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
        )
        try:
            Base.metadata.create_all(engine)
            db = Session(bind=engine)
            db.add(AuditLog(action="OK", severity="INFO", actor_hash="a",
                            source_ip="", request_summary="{}", result="SUCCESS"))
            db.commit()
            db.close()

            reader = Session(bind=engine)

            def _query(conn):
                from sqlalchemy import func, select

                return int(
                    conn.scalar(
                        select(func.count()).select_from(AuditLog)
                    )
                    or 0
                )

            count = open_read_snapshot(reader, _query)
            reader.close()
            assert count == 1
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_queue_pool_overflow_succeeds_with_owner_held(self) -> None:
        # pool_size=1, max_overflow=1: holding one owner connection still
        # leaves overflow capacity, so the snapshot succeeds immediately
        # through overflow (not rejected as exhausted).
        from sqlalchemy.pool import QueuePool

        from app.services.snapshot_helper import open_read_snapshot

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_overflow_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=1,
        )
        try:
            Base.metadata.create_all(engine)
            db = Session(bind=engine)
            db.add(AuditLog(action="OK", severity="INFO", actor_hash="a",
                            source_ip="", request_summary="{}", result="SUCCESS"))
            db.commit()
            db.close()

            # Hold one owner connection (fills the base slot).
            owner_conn = engine.connect()
            try:
                reader = Session(bind=engine)

                def _query(conn):
                    from sqlalchemy import func, select

                    return int(
                        conn.scalar(
                            select(func.count()).select_from(AuditLog)
                        )
                        or 0
                    )

                # Snapshot succeeds via overflow capacity.
                count = open_read_snapshot(reader, _query)
                reader.close()
                assert count == 1
            finally:
                owner_conn.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_queue_pool_no_overflow_rejects_with_owner_held(self) -> None:
        # pool_size=1, max_overflow=0: holding one owner connection exhausts
        # the full finite capacity (base + overflow = 1 + 0 = 1), so the
        # snapshot rejects immediately without waiting.
        from sqlalchemy.pool import QueuePool

        from app.services.snapshot_helper import (
            SnapshotUnavailable,
            open_read_snapshot,
        )

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_no_overflow_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=0,
        )
        try:
            Base.metadata.create_all(engine)
            owner_conn = engine.connect()
            try:
                reader = Session(bind=engine)
                with pytest.raises(SnapshotUnavailable) as exc_info:
                    open_read_snapshot(reader, lambda conn: None)
                assert "exhausted" in str(exc_info.value).lower()
                reader.close()
            finally:
                owner_conn.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)

    def test_queue_pool_unlimited_overflow_succeeds_with_multiple_holders(
        self,
    ) -> None:
        # max_overflow=-1 (unlimited): holding multiple connections never
        # pre-rejects for exhaustion; engine.connect() can always obtain an
        # overflow connection.
        from sqlalchemy.pool import QueuePool

        from app.services.snapshot_helper import open_read_snapshot

        db_path = os.path.join(
            tempfile.gettempdir(),
            f"auto_trade_test_snapshot_unlimited_{os.getpid()}.db",
        )
        if os.path.exists(db_path):
            os.unlink(db_path)
        engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=QueuePool,
            pool_size=1,
            max_overflow=-1,
        )
        try:
            Base.metadata.create_all(engine)
            db = Session(bind=engine)
            db.add(AuditLog(action="OK", severity="INFO", actor_hash="a",
                            source_ip="", request_summary="{}", result="SUCCESS"))
            db.commit()
            db.close()

            # Hold several connections beyond base size.
            holders = [engine.connect() for _ in range(3)]
            try:
                reader = Session(bind=engine)

                def _query(conn):
                    from sqlalchemy import func, select

                    return int(
                        conn.scalar(
                            select(func.count()).select_from(AuditLog)
                        )
                        or 0
                    )

                count = open_read_snapshot(reader, _query)
                reader.close()
                assert count == 1
            finally:
                for h in holders:
                    h.close()
        finally:
            engine.dispose()
            if os.path.exists(db_path):
                os.unlink(db_path)
