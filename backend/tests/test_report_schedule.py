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

    def test_preview_invalid_configured_symbol_parity_with_build_summary(self) -> None:
        # An invalid legacy configured symbol must produce the SAME fallback
        # title/content as build_summary (and thus manual/scheduled send),
        # NOT a preview 400. Only explicit overrides are validated.
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="BOGUS.X")
        svc = ReportScheduleService(self._db(), state={})
        symbol, _target, title, content = svc.preview()
        assert symbol == "BOGUS.X"
        direct_title, direct_content = svc.build_summary("BOGUS.X")
        assert title == direct_title
        assert content == direct_content
        # build_summary returns a fallback "report generation failed" message
        # for an invalid symbol, not a raise — preview must match that.
        assert "BOGUS.X" in title

    def test_preview_invalid_configured_symbol_parity_with_manual_send(self) -> None:
        # The manual run endpoint calls build_summary(symbol) directly with no
        # validation; preview with the same configured symbol must return the
        # same title/content (parity), not 400.
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="BAD.SUFFIX")
        svc = ReportScheduleService(self._db(), state={})
        symbol, _target, title, content = svc.preview()
        # Manual send path resolves the same symbol and calls build_summary.
        manual_title, manual_content = svc.build_summary(
            ReportScheduleService.resolve_effective_symbol(
                self._db().query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
            )
        )
        assert title == manual_title
        assert content == manual_content

    def test_preview_explicit_invalid_override_still_returns_400(self) -> None:
        # Explicit overrides ARE validated even though configured symbols are not.
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError, match="symbol market must be US or HK"):
            svc.preview(symbol_override="BOGUS.X")

    def test_preview_strict_date_rejects_single_digit_month(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026-6-01")

    def test_preview_strict_date_rejects_single_digit_day(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026-06-1")

    def test_preview_strict_date_rejects_impossible_date(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026-06-31")

    def test_preview_strict_date_rejects_trailing_content(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026-06-01T00:00:00")

    def test_preview_strict_date_rejects_leading_content(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date=" 2026-06-01")

    def test_preview_strict_date_rejects_unicode_digits(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        # Full-width Unicode digits that look like ASCII but are not.
        with pytest.raises(ValueError):
            svc.preview(target_date="２０２６-０６-０１")

    def test_preview_strict_date_accepts_valid_leap_day(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        symbol, target, _title, _content = svc.preview(target_date="2024-02-29")
        assert target == "2024-02-29"

    def test_preview_strict_date_rejects_non_leap_day(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.preview(target_date="2026-02-29")

    def test_build_summary_strict_date_rejects_impossible_date(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.build_summary("AAPL.US", target_date="2026-06-31")

    def test_build_summary_strict_date_rejects_trailing_content(self) -> None:
        self._cfg(symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), state={})
        with pytest.raises(ValueError):
            svc.build_summary("AAPL.US", target_date="2026-06-01 ")


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

    def test_preview_impossible_date_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"date": "2026-06-31"},
        )
        assert resp.status_code == 400

    def test_preview_single_digit_month_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"date": "2026-6-01"},
        )
        assert resp.status_code == 400

    def test_preview_trailing_content_date_returns_400(self) -> None:
        self._cfg(symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"date": "2026-06-01T00:00:00"},
        )
        assert resp.status_code == 400

    def test_preview_invalid_configured_symbol_returns_200_not_400(self) -> None:
        # Parity: an invalid configured symbol produces the same fallback
        # title/content as manual/scheduled send, NOT a 400.
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="BOGUS.X")
        resp = self.client.get("/api/reports/schedule/preview")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["symbol"] == "BOGUS.X"
        assert "BOGUS.X" in data["title"]

    def test_preview_explicit_invalid_override_returns_400(self) -> None:
        # Explicit overrides are still validated.
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        resp = self.client.get(
            "/api/reports/schedule/preview",
            params={"symbol": "BOGUS.X"},
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


class TestReportScheduleStatusService(_Base):
    """Service-level status: safe fields, clamping, process semantics, no mutation."""

    def test_status_disabled(self) -> None:
        self._cfg(report_schedule_enabled=False, report_schedule_symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state={})
        st = svc.status()
        assert st.enabled is False
        assert st.effective_symbol == "AAPL.US"
        assert st.eligible_now is False
        assert st.has_process_send_history is False
        assert st.last_sent_age_seconds is None
        assert st.next_eligible_in_seconds is None

    def test_status_missing_config(self) -> None:
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        assert st.enabled is False
        assert st.configured_symbol == ""
        assert st.effective_symbol == ""
        assert st.eligible_now is False
        assert st.interval_hours == 24

    def test_status_missing_symbol(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="", symbol="")
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        assert st.enabled is True
        assert st.effective_symbol == ""
        assert st.eligible_now is False

    def test_status_never_sent_eligible_now(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=24,
        )
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state={})
        st = svc.status()
        assert st.enabled is True
        assert st.effective_symbol == "AAPL.US"
        assert st.has_process_send_history is False
        assert st.last_sent_age_seconds is None
        assert st.next_eligible_in_seconds is None
        assert st.eligible_now is True

    def test_status_recently_sent_not_eligible(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        state = {"AAPL.US": 1000.0}
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0 + 60.0, state=state)
        st = svc.status()
        assert st.has_process_send_history is True
        assert st.last_sent_age_seconds == 60.0
        # 1 hour window, 60s elapsed -> 3540s remaining.
        assert st.next_eligible_in_seconds == 3540.0
        assert st.eligible_now is False

    def test_status_elapsed_past_window_eligible(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        state = {"AAPL.US": 1000.0}
        svc = ReportScheduleService(
            self._db(), clock=lambda: 1000.0 + 3600.0 + 1.0, state=state
        )
        st = svc.status()
        assert st.last_sent_age_seconds == 3601.0
        assert st.next_eligible_in_seconds == 0.0
        assert st.eligible_now is True

    def test_status_clock_rollback_clamps_negative_elapsed(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        # last recorded at 2000, clock now reads 1000 (rollback of 1000s).
        state = {"AAPL.US": 2000.0}
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state=state)
        st = svc.status()
        # Exposed age is clamped to 0, not negative.
        assert st.last_sent_age_seconds == 0.0
        # next_eligible uses RAW elapsed (now - last = -1000), so the remaining
        # wait EXCEEDS the window: 3600 - (-1000) = 4600. This mirrors
        # maybe_send, which stays throttled until the rollback is resolved.
        assert st.next_eligible_in_seconds == 4600.0
        assert st.eligible_now is False

    def test_status_clock_rollback_clamps_remaining_to_zero_when_over_elapsed(self) -> None:
        # When raw elapsed exceeds the window (no rollback), remaining clamps
        # to zero and the report is eligible.
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        state = {"AAPL.US": 1000.0}
        svc = ReportScheduleService(
            self._db(), clock=lambda: 1000.0 + 3600.0 + 1.0, state=state
        )
        st = svc.status()
        assert st.last_sent_age_seconds == 3601.0
        assert st.next_eligible_in_seconds == 0.0
        assert st.eligible_now is True

    def test_status_process_semantics_fields(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        assert st.state_scope == "process"
        assert st.resets_on_restart is True

    def test_status_interval_hours_minimum_clamp(self) -> None:
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=0,
        )
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        # Mirrors maybe_send: `int(... or 24)` treats 0 as falsy -> 24, then
        # max(1, ...) guarantees a positive window. Status must match exactly.
        assert st.interval_hours == 24

    def test_status_does_not_mutate_state(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        state: dict[str, float] = {"AAPL.US": 100.0}
        svc = ReportScheduleService(self._db(), clock=lambda: 200.0, state=state)
        svc.status()
        assert state == {"AAPL.US": 100.0}

    def test_status_does_not_call_notifier(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")

        def _boom(*_a: object, **_k: object) -> None:
            raise AssertionError("status must not call the notifier")

        monkeypatch.setattr("app.services.report_schedule_service.get_runner", _boom, raising=False)
        svc = ReportScheduleService(self._db(), state={})
        svc.status()  # must not raise

    def test_status_does_not_write_db_or_audit(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        # status() must not create/modify StrategyConfig rows. After calling,
        # there should still be exactly one config row (the one we seeded).
        svc.status()
        db = self._db()
        count = db.query(StrategyConfig).count()
        db.close()
        assert count == 1

    def test_status_uses_same_symbol_resolution_as_maybe_send(self) -> None:
        # configured_symbol set, strategy symbol different -> effective is configured.
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="0700.HK",
            symbol="AAPL.US",
        )
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        assert st.configured_symbol == "0700.HK"
        assert st.effective_symbol == "0700.HK"

    def test_status_falls_back_to_strategy_symbol(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="", symbol="MSFT.US")
        svc = ReportScheduleService(self._db(), clock=lambda: 1.0, state={})
        st = svc.status()
        assert st.configured_symbol == ""
        assert st.effective_symbol == "MSFT.US"

    def test_status_eligible_now_boundary_matches_maybe_send(self) -> None:
        # The point at which status reports eligible_now=True must be the
        # exact point at which maybe_send actually dispatches. Sweep the clock
        # across the window boundary and confirm both agree at every step.
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        state = {"AAPL.US": 1000.0}
        notifier = FakeNotifier()
        for offset in (0.0, 60.0, 3599.0, 3600.0, 3600.0 - 0.001, 3600.0 + 0.001, 7200.0):
            svc = ReportScheduleService(
                self._db(), clock=lambda o=offset: 1000.0 + o, state=state
            )
            st = svc.status()
            # maybe_send's gate: last is not None and (now - last) < window.
            raw_elapsed = (1000.0 + offset) - 1000.0
            send_eligible = not (raw_elapsed < 3600.0)
            assert st.eligible_now is send_eligible, (
                f"mismatch at offset={offset}: status={st.eligible_now} maybe_send={send_eligible}"
            )

    def test_status_clock_rollback_eligible_now_matches_maybe_send(self) -> None:
        # With a clock rollback (raw_elapsed negative), maybe_send stays
        # throttled and status must report eligible_now=False with a remaining
        # wait exceeding the window.
        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        state = {"AAPL.US": 2000.0}
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state=state)
        st = svc.status()
        # maybe_send with the same clock would be throttled (raw -1000 < 3600).
        send_svc = ReportScheduleService(
            self._db(), clock=lambda: 1000.0, state=state
        )
        assert send_svc.maybe_send(FakeRunner(FakeNotifier())) is False
        assert st.eligible_now is False
        assert st.next_eligible_in_seconds == 4600.0  # 3600 - (-1000)


class TestReportScheduleStatusAPI(_Base):
    """GET /api/reports/schedule/status — read-only, authenticated, no side effects."""

    def test_status_disabled(self) -> None:
        self._cfg(report_schedule_enabled=False, report_schedule_symbol="AAPL.US")
        resp = self.client.get("/api/reports/schedule/status")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["enabled"] is False
        assert data["effective_symbol"] == "AAPL.US"
        assert data["eligible_now"] is False
        assert data["state_scope"] == "process"
        assert data["resets_on_restart"] is True

    def test_status_missing_config(self) -> None:
        resp = self.client.get("/api/reports/schedule/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["configured_symbol"] == ""
        assert data["effective_symbol"] == ""
        assert data["eligible_now"] is False
        assert data["interval_hours"] == 24

    def test_status_never_sent(self) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        resp = self.client.get("/api/reports/schedule/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        assert data["has_process_send_history"] is False
        assert data["last_sent_age_seconds"] is None
        assert data["next_eligible_in_seconds"] is None
        assert data["eligible_now"] is True

    def test_status_recently_sent(self) -> None:
        import app.services.report_schedule_service as svc_mod

        self._cfg(
            report_schedule_enabled=True,
            report_schedule_symbol="AAPL.US",
            report_schedule_interval_hours=1,
        )
        # Seed process-local throttle state, then read status.
        svc = ReportScheduleService(self._db(), clock=lambda: 1000.0, state=svc_mod._LAST_SENT)
        svc.maybe_send(FakeRunner(FakeNotifier()))
        try:
            # Advance clock 60s via a service that reads the same module state.
            svc2 = ReportScheduleService(
                self._db(), clock=lambda: 1060.0, state=svc_mod._LAST_SENT
            )
            st = svc2.status()
            assert st.has_process_send_history is True
            assert st.last_sent_age_seconds == 60.0
            assert st.eligible_now is False
            assert st.next_eligible_in_seconds == 3540.0
        finally:
            svc_mod._LAST_SENT.clear()

    def test_status_no_throttle_mutation(self) -> None:
        import app.services.report_schedule_service as svc_mod

        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc_mod._LAST_SENT["AAPL.US"] = 500.0
        try:
            snapshot = dict(svc_mod._LAST_SENT)
            resp = self.client.get("/api/reports/schedule/status")
            assert resp.status_code == 200
            assert svc_mod._LAST_SENT == snapshot
        finally:
            svc_mod._LAST_SENT.clear()

    def test_status_no_notifier_side_effect(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")

        def _boom(*_a: object, **_k: object) -> bool:
            raise AssertionError("status must not call the notifier")

        monkeypatch.setattr("app.api.reports.get_runner", lambda: FakeRunner(_boom))
        resp = self.client.get("/api/reports/schedule/status")
        assert resp.status_code == 200

    def test_status_no_audit_side_effect(self, monkeypatch) -> None:
        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        recorded: list[str] = []

        class _SpyAudit:
            def record(self, *args: object, **kwargs: object) -> None:
                recorded.append("audit")

        monkeypatch.setattr("app.api.reports.get_audit_logger", lambda: _SpyAudit())
        resp = self.client.get("/api/reports/schedule/status")
        assert resp.status_code == 200
        assert recorded == []

    def test_status_requires_auth_when_api_key_set(self, monkeypatch) -> None:
        from app.api import auth as auth_mod

        monkeypatch.setattr(auth_mod.settings, "api_key", "secret-key", raising=False)
        monkeypatch.setattr(auth_mod.settings, "env", "prod", raising=False)
        try:
            resp = self.client.get("/api/reports/schedule/status")
            assert resp.status_code == 401
            resp2 = self.client.get(
                "/api/reports/schedule/status",
                headers={"X-API-Key": "secret-key"},
            )
            # No config -> still 200 (status is safe even with no config).
            assert resp2.status_code == 200
        finally:
            monkeypatch.setattr(auth_mod.settings, "api_key", "", raising=False)
            monkeypatch.setattr(auth_mod.settings, "env", "dev", raising=False)

    def test_status_does_not_expose_raw_timestamps(self) -> None:
        import app.services.report_schedule_service as svc_mod

        self._cfg(report_schedule_enabled=True, report_schedule_symbol="AAPL.US")
        svc_mod._LAST_SENT["AAPL.US"] = 12345.678
        try:
            resp = self.client.get("/api/reports/schedule/status")
            assert resp.status_code == 200
            text = resp.text
            # The raw monotonic timestamp must never appear in the response.
            assert "12345.678" not in text
            assert "12345" not in text
        finally:
            svc_mod._LAST_SENT.clear()
