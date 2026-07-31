"""Notification log — sink, service, multi_channel hook, API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_notification_log_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.notifiers.multi_channel import MultiChannelNotifier
from app.database import get_db
from app.main import app
from app.models import Base, NotificationLog
from app.schemas import NotificationDailyPoint
from app.services.notification_log_service import NotificationLogService, NotificationLogSink


class _FakeNotifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok = ok

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        return self.ok

    def notify_order(self, *a, **k) -> bool:
        return True

    def notify_fill(self, *a, **k) -> bool:
        return True

    def notify_risk_event(self, *a, **k) -> bool:
        return True


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
        db.query(NotificationLog).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _factory(self):
        return lambda: Session(bind=self.engine)

    def _add_log(self, title: str, created_at, success: bool = True, severity: str = "INFO", error: str = "") -> None:
        from datetime import datetime, timezone

        db = self._db()
        db.add(
            NotificationLog(
                title=title,
                content="",
                severity=severity,
                success=success,
                error=error,
                created_at=created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc),
            )
        )
        db.commit()
        db.close()


class TestNotificationSinkAndService(_Base):
    def test_sink_records(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("title", "body", "WARNING", True, "")
        rows = self._db().query(NotificationLog).all()
        assert len(rows) == 1
        assert rows[0].title == "title"
        assert rows[0].severity == "WARNING"
        assert rows[0].success is True

    def test_sink_swallows_errors(self) -> None:
        def bad_factory():
            raise RuntimeError("db gone")

        sink = NotificationLogSink(bad_factory)
        sink.record("t", "c", "INFO", True)  # must not raise

    def test_service_list_and_filter(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("a", "x", "INFO", True)
        sink.record("b", "y", "CRITICAL", False, "boom")
        svc = NotificationLogService(self._db())
        assert svc.list_logs().total == 2
        assert svc.list_logs(severity="CRITICAL").total == 1
        assert svc.list_logs(severity="CRITICAL").items[0].success is False

    def test_multi_channel_invokes_sink(self) -> None:
        captured: list[tuple] = []
        sink = lambda t, c, s, ok, err: captured.append((t, s, ok, err))  # noqa: E731
        notifier = MultiChannelNotifier([(_FakeNotifier(ok=True), "INFO")], sink=sink)
        assert notifier.send("hello", "world", "INFO") is True
        assert captured == [("hello", "INFO", True, "")]

    def test_multi_channel_dedup_persists_only_first_success(self) -> None:
        sink = NotificationLogSink(self._factory())
        notifier = MultiChannelNotifier(
            [(_FakeNotifier(ok=True), "INFO")],
            sink=sink.record,
            dedup_window_seconds=300.0,
        )

        assert notifier.send("hello", "world", "INFO") is True
        assert notifier.send("hello", "world", "INFO") is True

        db = self._db()
        try:
            assert db.query(NotificationLog).count() == 1
        finally:
            db.close()
        assert notifier.dedup_suppressed_total == 1

    def test_service_q_search(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("hello", "world", "INFO", True)
        sink.record("goodbye", "moon", "CRITICAL", False, "boom")
        sink.record("other", "note", "WARNING", True)
        svc = NotificationLogService(self._db())
        assert svc.list_logs(q="hello").total == 1
        assert svc.list_logs(q="moon").total == 1
        assert svc.list_logs(q="boom").total == 1
        assert svc.list_logs(q="hell").total == 1  # hello only

    def test_service_success_filter(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("ok", "", "INFO", True)
        sink.record("bad", "", "CRITICAL", False, "err")
        svc = NotificationLogService(self._db())
        assert svc.list_logs(success=True).total == 1
        assert svc.list_logs(success=False).total == 1

    def test_service_date_range_filter(self) -> None:
        from datetime import datetime, timezone

        self._add_log("a", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc))
        self._add_log("b", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))

        svc = NotificationLogService(self._db())
        assert svc.list_logs(from_date="2026-06-16").total == 1
        assert svc.list_logs(from_date="2026-06-15", to_date="2026-06-15").total == 1
        assert svc.list_logs(to_date="2026-06-14").total == 0
        assert svc.list_logs(from_date="2026-06-15", to_date="2026-06-16").total == 2

    def test_api_q_and_success_filter(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("api ok", "body", "INFO", True)
        sink.record("api fail", "body", "CRITICAL", False, "err")
        resp = self.client.get("/api/notifications", params={"q": "fail", "success": "false"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["title"] == "api fail"

    def test_api_date_filter(self) -> None:
        from datetime import datetime, timezone

        self._add_log("a", datetime(2026, 6, 15, 10, 0, 0, tzinfo=timezone.utc))

        resp = self.client.get("/api/notifications", params={"from_date": "2026-06-16"})
        assert resp.json()["total"] == 0
        resp = self.client.get("/api/notifications", params={"from_date": "2026-06-15"})
        assert resp.json()["total"] == 1

    def test_api_invalid_date_returns_422(self) -> None:
        resp = self.client.get("/api/notifications", params={"from_date": "not-a-date"})
        assert resp.status_code == 422


class TestNotificationAPI(_Base):
    def test_endpoint(self) -> None:
        NotificationLogSink(self._factory()).record("api note", "body", "INFO", True)
        resp = self.client.get("/api/notifications")
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["title"] == "api note"

    def test_endpoint_severity_filter(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("a", "", "INFO", True)
        sink.record("b", "", "CRITICAL", False, "err")
        resp = self.client.get("/api/notifications", params={"severity": "CRITICAL"})
        assert resp.json()["total"] == 1

    def test_endpoint_severity_case_insensitive(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("a", "", "INFO", True)
        sink.record("b", "", "CRITICAL", False, "err")
        # lowercase 'info' must match stored 'INFO' (not silently return 0)
        resp = self.client.get("/api/notifications", params={"severity": "info"})
        assert resp.json()["total"] == 1
        assert resp.json()["items"][0]["severity"] == "INFO"

    def test_export_csv(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("hello", "world", "INFO", True)
        sink.record("goodbye", "moon", "CRITICAL", False, "boom")
        resp = self.client.get("/api/notifications/export", params={"format": "csv"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        body = resp.text
        assert "id,created_at,severity,success,title,content,error" in body
        assert "hello" in body
        assert "goodbye" in body

    def test_export_json(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("hello", "world", "INFO", True)
        resp = self.client.get("/api/notifications/export", params={"format": "json"})
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/json")
        data = resp.json()
        assert len(data) == 1
        assert data[0]["title"] == "hello"

    def test_export_respects_filters(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("hello", "world", "INFO", True)
        sink.record("goodbye", "moon", "CRITICAL", False, "boom")
        resp = self.client.get("/api/notifications/export", params={"format": "json", "severity": "CRITICAL"})
        data = resp.json()
        assert len(data) == 1
        assert data[0]["severity"] == "CRITICAL"

    def test_export_invalid_date_returns_422(self) -> None:
        resp = self.client.get("/api/notifications/export", params={"format": "csv", "from_date": "bad"})
        assert resp.status_code == 422

    def test_retry_failed_notification_succeeds(self, monkeypatch) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("fail", "body", "CRITICAL", False, "boom")
        log = self._db().query(NotificationLog).first()
        assert log is not None
        log_id = log.id

        fake_notifier = _FakeNotifier(ok=True)

        def fake_from_config(cls, *a, **k):
            return MultiChannelNotifier([(fake_notifier, "INFO")])

        monkeypatch.setattr(MultiChannelNotifier, "from_credential_config", classmethod(fake_from_config))

        resp = self.client.post(f"/api/notifications/{log_id}/retry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is True
        assert data["error"] == ""
        assert self._db().query(NotificationLog).count() == 1

    def test_retry_failed_notification_still_fails(self, monkeypatch) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("fail", "body", "CRITICAL", False, "boom")
        log = self._db().query(NotificationLog).first()
        assert log is not None
        log_id = log.id

        fake_notifier = _FakeNotifier(ok=False)

        def fake_from_config(cls, *a, **k):
            return MultiChannelNotifier([(fake_notifier, "INFO")])

        monkeypatch.setattr(MultiChannelNotifier, "from_credential_config", classmethod(fake_from_config))

        resp = self.client.post(f"/api/notifications/{log_id}/retry")
        assert resp.status_code == 200
        data = resp.json()
        assert data["success"] is False
        assert data["error"] == "retry failed"

    def test_retry_notification_not_found(self, monkeypatch) -> None:
        fake_notifier = _FakeNotifier(ok=True)

        def fake_from_config(cls, *a, **k):
            return MultiChannelNotifier([(fake_notifier, "INFO")])

        monkeypatch.setattr(MultiChannelNotifier, "from_credential_config", classmethod(fake_from_config))

        resp = self.client.post("/api/notifications/999/retry")
        assert resp.status_code == 404


class TestNotificationStats(_Base):
    def test_service_totals_and_rate(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("ok1", "", "INFO", True)
        sink.record("ok2", "", "WARNING", True)
        sink.record("bad", "", "CRITICAL", False, "boom")
        stats = NotificationLogService(self._db()).statistics()
        assert stats.total == 3
        assert stats.success == 2
        assert stats.failed == 1
        assert stats.success_rate == 66.67
        assert sum(b.total for b in stats.by_channel) == stats.total
        assert sum(b.total for b in stats.by_severity) == stats.total

    def test_service_empty_log(self) -> None:
        stats = NotificationLogService(self._db()).statistics()
        assert stats.total == 0
        assert stats.success == 0
        assert stats.failed == 0
        assert stats.success_rate == 0.0
        assert stats.by_severity == []
        assert stats.by_channel == []
        assert stats.daily == []

    def test_service_severity_breakdown(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("a", "", "INFO", True)
        sink.record("b", "", "INFO", False, "x")
        sink.record("c", "", "CRITICAL", True)
        stats = NotificationLogService(self._db()).statistics()
        sev = {b.key: b for b in stats.by_severity}
        assert sev["INFO"].total == 2
        assert sev["INFO"].success == 1
        assert sev["INFO"].failed == 1
        assert sev["CRITICAL"].total == 1
        assert sev["CRITICAL"].success == 1

    def test_service_channel_breakdown_from_error(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("ok", "", "INFO", True)  # no error -> unknown
        sink.record("bad1", "", "CRITICAL", False, "ServerChanNotifier: http 500")
        sink.record("bad2", "", "CRITICAL", False, "TelegramNotifier: timeout")
        sink.record("bad3", "", "CRITICAL", False, "SomethingElse: boom")
        stats = NotificationLogService(self._db()).statistics()
        chan = {b.key: b for b in stats.by_channel}
        assert chan["serverchan"].total == 1
        assert chan["serverchan"].failed == 1
        assert chan["telegram"].total == 1
        # success + unrecognized failure prefix both bucket under unknown
        assert chan["unknown"].total == 2
        assert sum(b.total for b in stats.by_channel) == stats.total

    def test_service_daily_trend_deterministic(self) -> None:
        from datetime import datetime, timezone

        self._add_log("a", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add_log("b", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))
        self._add_log("c", datetime(2026, 6, 16, 11, 0, 0, tzinfo=timezone.utc))
        stats = NotificationLogService(self._db()).statistics()
        assert [p.date for p in stats.daily] == ["2026-06-14", "2026-06-16"]
        assert stats.daily[1].total == 2

    def test_service_date_filter_bounds_stats(self) -> None:
        from datetime import datetime, timezone

        self._add_log("a", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add_log("b", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))
        stats = NotificationLogService(self._db()).statistics(from_date="2026-06-16")
        assert stats.total == 1
        assert stats.daily == [
            NotificationDailyPoint(date="2026-06-16", total=1, success=1, failed=0)
        ]

    def test_api_stats(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("api ok", "body", "INFO", True)
        sink.record("api fail", "body", "CRITICAL", False, "WebhookNotifier: 500")
        resp = self.client.get("/api/notifications/stats")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 2
        assert data["success"] == 1
        assert data["failed"] == 1
        assert data["success_rate"] == 50.0
        # stats must not leak payload/content/error
        for forbidden in ("title", "content", "error"):
            assert forbidden not in data
        chan = {b["key"]: b for b in data["by_channel"]}
        assert chan["webhook"]["total"] == 1
        assert chan["webhook"]["failed"] == 1

    def test_api_stats_respects_date_filter(self) -> None:
        from datetime import datetime, timezone

        self._add_log("a", datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
        self._add_log("b", datetime(2026, 6, 16, 10, 0, 0, tzinfo=timezone.utc))
        resp = self.client.get("/api/notifications/stats", params={"from_date": "2026-06-16"})
        assert resp.json()["total"] == 1
        assert [p["date"] for p in resp.json()["daily"]] == ["2026-06-16"]

    def test_api_stats_severity_filter_case_insensitive(self) -> None:
        sink = NotificationLogSink(self._factory())
        sink.record("a", "", "INFO", True)
        sink.record("b", "", "CRITICAL", True)
        resp = self.client.get("/api/notifications/stats", params={"severity": "critical"})
        data = resp.json()
        assert data["total"] == 1
        assert data["by_severity"] == [{"key": "CRITICAL", "total": 1, "success": 1, "failed": 0}]

    def test_api_stats_invalid_date_returns_422(self) -> None:
        resp = self.client.get("/api/notifications/stats", params={"to_date": "nope"})
        assert resp.status_code == 422
