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

    def test_multi_channel_sink_records_failure(self) -> None:
        captured: list[tuple] = []
        sink = lambda t, c, s, ok, err: captured.append((t, s, ok, err))  # noqa: E731
        notifier = MultiChannelNotifier([(_FakeNotifier(ok=False), "INFO")], sink=sink)
        notifier.send("warn", "body", "WARNING")
        assert captured[0][2] is False  # success False
        assert captured[0][1] == "WARNING"


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
