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


class TestReportSchedulePreviewService(_Base):
    """Service-level preview: effective-symbol resolution + date injection, no side effects."""

    def test_preview_uses_configured_symbol_fallback(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="", symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, target, title, content = svc.preview()
        assert symbol == "AAPL.US"
        assert title == f"交易日报 · AAPL.US"
        assert isinstance(content, str) and content

    def test_preview_uses_report_schedule_symbol_when_set(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="0700.HK",
            symbol="AAPL.US",
        )
        svc = ReportScheduleService(self._db(), state={})
        symbol, _target, title, _content = svc.preview()
        assert symbol == "0700.HK"
        assert title == "交易日报 · 0700.HK"

    def test_preview_symbol_override_takes_precedence(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="0700.HK", symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, _target, title, _content = svc.preview(symbol_override="MSFT.US")
        assert symbol == "MSFT.US"
        assert title == "交易日报 · MSFT.US"

    def test_preview_override_normalizes_case_and_whitespace(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, _target, _title, _content = svc.preview(symbol_override="  msft.us  ")
        assert symbol == "MSFT.US"

    def test_preview_explicit_target_date(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, target, _title, _content = svc.preview(target_date="2026-06-01")
        assert symbol == "AAPL.US"
        assert target == "2026-06-01"

    def test_preview_default_target_date_is_today_utc(self) -> None:
        from datetime import datetime, timezone

        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        _symbol, target, _title, _content = svc.preview()
        assert target == datetime.now(timezone.utc).date().isoformat()

    def test_preview_exact_title_and_content_match_build_summary(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, target, title, content = svc.preview(target_date="2026-06-01")
        # Must equal what build_summary produces directly for the same inputs.
        direct_title, direct_content = svc.build_summary(symbol, target_date=target)
        assert title == direct_title
        assert content == direct_content

    def test_preview_missing_symbol_raises(self) -> None:
        # No config row at all -> no effective symbol.
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError, match="no effective report symbol"):
            svc.preview()

    def test_preview_empty_config_symbol_raises(self) -> None:
        self._cfg(report_schedule_symbol="", symbol="")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError, match="no effective report symbol"):
            svc.preview()

    def test_preview_invalid_override_symbol_raises(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError, match="symbol market must be US or HK"):
            svc.preview(symbol_override="AAPL")  # missing market suffix

    def test_preview_invalid_override_market_raises(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError, match="symbol market must be US or HK"):
            svc.preview(symbol_override="7203.JP")

    def test_preview_invalid_target_date_raises(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026/06/01")

    def test_preview_invalid_target_date_format_raises(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="not-a-date")

    def test_preview_does_not_mutate_throttle_state(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        state: dict[str, float] = {}
        svc = ReportScheduleService(self._db(), state=state)
        svc.preview()
        assert state == {}

    def test_preview_does_not_call_notifier_or_runner(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        # If preview ever imports/touches the runner, this guard fails the test.
        called: list[str] = []

        def _boom(*_a: object, **_k: object) -> None:
            called.append("runner")
            raise AssertionError("preview must not access the runner")

        monkeypatch.setattr("app.services.report_schedule_service.get_runner", _boom, raising=False)
        svc = ReportScheduleService(self._db(), state={})
        svc.preview()
        assert called == []

    def test_build_summary_preserves_default_date_when_target_none(self) -> None:
        # Existing callers pass no target_date; behavior must be unchanged.
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        title_default, content_default = svc.build_summary("AAPL.US")
        title_explicit, content_explicit = svc.build_summary(
            "AAPL.US", target_date=None
        )
        assert title_default == title_explicit
        assert content_default == content_explicit


class TestReportSchedulePreviewAPI(_Base):
    """GET /api/reports/schedule/preview — read-only, authenticated, no side effects."""

    def test_preview_configured_fallback(self) -> None:
        self._cfg(report_schedule_symbol="", symbol="AAPL.US")
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["title"] == "交易日报 · AAPL.US"
        assert "content" in data and isinstance(data["content"], str)
        # target_date is YYYY-MM-DD today (UTC).
        from datetime import datetime, timezone

        assert data["target_date"] == datetime.now(timezone.utc).date().isoformat()

    def test_preview_symbol_override_and_date(self) -> None:
        self._cfg(report_schedule_symbol="0700.HK", symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"symbol": "MSFT.US", "date": "2026-06-01"},
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["symbol"] == "MSFT.US"
        assert data["target_date"] == "2026-06-01"
        assert data["title"] == "交易日报 · MSFT.US"

    def test_preview_exact_title_content_matches_service(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"date": "2026-06-01"},
        )
        assert resp.status_code == 200
        data = resp.json()
        svc = ReportScheduleService(self._db(), state={})
        title, content = svc.build_summary("AAPL.US", target_date="2026-06-01")
        assert data["title"] == title
        assert data["content"] == content

    def test_preview_invalid_symbol_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"symbol": "AAPL"},
        )
        assert resp.status_code == 400
        assert resp.json()["detail"] == "symbol market must be US or HK"

    def test_preview_invalid_market_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"symbol": "7203.JP"},
        )
        assert resp.status_code == 400

    def test_preview_missing_effective_symbol_returns_400(self) -> None:
        # No config row -> no effective symbol.
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no effective report symbol configured"

    def test_preview_empty_config_symbol_returns_400(self) -> None:
        self._cfg(report_schedule_symbol="", symbol="")
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 400
        assert resp.json()["detail"] == "no effective report symbol configured"

    def test_preview_invalid_date_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"date": "2026/06/01"},
        )
        assert resp.status_code == 400

    def test_preview_requires_auth_when_api_key_set(self, monkeypatch) -> None:
        # Simulate a production-like api key: auth dependency must reject.
        from app.api import auth as auth_mod

        monkeypatch.setattr(auth_mod.settings, "api_key", "secret-key", raising=False)
        monkeypatch.setattr(auth_mod.settings, "env", "prod", raising=False)
        try:
            resp = self.client.get("/api/reports/schedule/preview")
            assert resp.status_code == 401
            # With a valid key it passes.
            resp2 = self.client.get(
                "/api/reports/schedule/preview",
                headers={"X-API-Key": "secret-key"},
            )
            # No config -> 400, but that proves auth passed.
            assert resp2.status_code == 400
        finally:
            # settings is a shared singleton; restore to empty (dev/test mode).
            monkeypatch.setattr(auth_mod.settings, "api_key", "", raising=False)
            monkeypatch.setattr(auth_mod.settings, "env", "dev", raising=False)

    def test_preview_no_notifier_side_effect(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        # If the endpoint dispatches a notification, this fails.
        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("preview must not call the notifier")

        monkeypatch.setattr("app.api.reports.get_runner", lambda: FakeRunner(_boom))
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 200

    def test_preview_no_audit_side_effect(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        recorded: list[str] = []

        class _SpyAudit:
            def record(self, *args: object, **kwargs: object) -> None:
                recorded.append("audit")

        # The preview endpoint does not depend on the audit logger, but if it
        # ever does, this spy will catch it.
        monkeypatch.setattr("app.api.reports.get_audit_logger", lambda: _SpyAudit())
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 200
        assert recorded == []

    def test_preview_no_throttle_mutation(self, monkeypatch) -> None:
        import app.services.report_schedule_service as svc_mod

        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        snapshot_before = dict(svc_mod._LAST_SENT)
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 200
        assert svc_mod._LAST_SENT == snapshot_before
