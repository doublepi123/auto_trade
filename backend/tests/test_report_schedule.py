"""Scheduled performance reports — service + manual endpoint. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile
from types import SimpleNamespace

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_report_schedule_{os.getpid()}.db"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, StrategyConfig
from app.services.report_schedule_service import ReportScheduleService


class FakeNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []
        self.return_true = True

    def send(self, title: str, content: str, severity: str = "INFO") -> bool:
        self.calls.append((title, content, severity))
        return self.return_true


class FakeRunner:
    def __init__(self, notifier: object | None) -> None:
        self.notifier = notifier


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
        db.query(StrategyConfig).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _cfg(self, **kw) -> None:
        db = self._db()
        db.add(StrategyConfig(
            symbol=kw.get("symbol", "AAPL.US"),
            report_schedule_enabled=kw.get("report_schedule_enabled", False),
            report_schedule_interval_hours=kw.get("report_schedule_interval_hours", 24),
            report_schedule_symbol=kw.get("report_schedule_symbol", ""),
        ))
        db.commit()
        db.close()


class TestReportScheduleService(_Base):
    def test_disabled_does_not_send(self) -> None:
        self._cfg(report_schedule_enabled=False, report_schedule_symbol="AAPL.US")
        notifier = FakeNotifier()
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state={})
        assert svc.maybe_send(FakeRunner(notifier)) is False
        assert notifier.calls == []

    def test_enabled_sends_then_throttles(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US", report_schedule_interval_hours=1)
        notifier = FakeNotifier()
        ticks = [1000.0]
        svc = ReportScheduleService(self._db(), clock=lambda: ticks[0], state={})
        assert svc.maybe_send(FakeRunner(notifier)) is True
        assert len(notifier.calls) == 1
        # Within the interval -> throttled.
        ticks[0] = 1000.0 + 60.0
        assert svc.maybe_send(FakeRunner(notifier)) is False
        # Past the interval -> sends again.
        ticks[0] = 1000.0 + 3600 + 1
        assert svc.maybe_send(FakeRunner(notifier)) is True
        assert len(notifier.calls) == 2

    def test_missing_symbol_does_not_send(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="", symbol="")
        notifier = FakeNotifier()
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        assert svc.maybe_send(FakeRunner(notifier)) is False

    def test_no_notifier_does_not_send(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        assert svc.maybe_send(FakeRunner(None)) is False

    def test_build_summary_returns_strings(self) -> None:
        # Empty DB -> report has no trades; summary still builds.
        title, content = ReportScheduleService(self._db()).build_summary("AAPL.US")
        assert isinstance(title, str) and isinstance(content, str)
        assert "AAPL.US" in title

    def test_build_summary_reports_unresolved_quality_before_no_trades(
        self,
        monkeypatch,
    ) -> None:
        report = SimpleNamespace(
            statistics_quality=SimpleNamespace(
                status="UNRESOLVED",
                omitted_day_count=1,
                unresolved_issue_count=2,
            ),
            metrics=SimpleNamespace(total_trades=0),
        )
        fake_service = SimpleNamespace(get_daily_report=lambda *_args: report)
        monkeypatch.setattr(
            "app.services.report_schedule_service.ReportService",
            lambda _db: fake_service,
        )

        _title, content = ReportScheduleService(self._db()).build_summary(
            "AAPL.US"
        )

        assert "统计未完成" in content
        assert "今日暂无成交" not in content


class TestReportScheduleAPI(_Base):
    def test_manual_run_endpoint(self, monkeypatch) -> None:
        notifier = FakeNotifier()
        monkeypatch.setattr("app.api.reports.get_runner", lambda: FakeRunner(notifier))
        resp = self.client.post("/api/reports/schedule/run")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["sent"] is True
        assert len(notifier.calls) == 1
        assert "title" in data

    def test_manual_run_endpoint_no_notifier(self, monkeypatch) -> None:
        monkeypatch.setattr("app.api.reports.get_runner", lambda: FakeRunner(None))
        resp = self.client.post("/api/reports/schedule/run")
        assert resp.status_code == 200
        assert resp.json()["sent"] is False
