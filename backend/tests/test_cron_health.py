"""Cron job health service and API — process-local, fake-clock, read-only.

Covers: success/failure recording, reset/isolation, stale boundary, disabled
behavior, sanitized error projection, enabled-provider failure isolation,
and the relevant main/startup registration behavior.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.api import cron_health as cron_health_api
from app.config import settings
from app.main import app
from app.services.cron_health_service import (
    CronHealthService,
    JobHealthSnapshot,
    get_cron_health_service,
    set_cron_health_service,
)


class _FakeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Generator[tuple[CronHealthService, _FakeClock], None, None]:
    clock = _FakeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
    service = CronHealthService(now=clock)
    set_cron_health_service(service)
    monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: service)
    yield service, clock
    set_cron_health_service(None)


class TestCronHealthServiceRegistration:
    def test_register_is_idempotent_preserving_counters(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job_a", expected_interval_seconds=60.0)
        service.record_success("job_a")
        service.record_success("job_a")
        assert service.snapshot()[0].tick_count == 2
        # Re-register with new metadata; counters preserved.
        service.register("job_a", expected_interval_seconds=120.0)
        row = service.snapshot()[0]
        assert row.tick_count == 2
        assert row.expected_interval_seconds == 120.0

    def test_snapshot_sorted_by_name(self, fake_service) -> None:
        service, _ = fake_service
        service.register("zeta", expected_interval_seconds=10.0)
        service.register("alpha", expected_interval_seconds=10.0)
        names = [row.name for row in service.snapshot()]
        assert names == ["alpha", "zeta"]


class TestCronHealthServiceSuccess:
    def test_record_success_updates_last_success_and_tick_count(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        clock.advance(5)
        service.record_success("job")
        clock.advance(10)
        row = service.snapshot()[0]
        assert row.tick_count == 1
        assert row.last_success_at == clock.now - timedelta(seconds=10)
        assert row.failure_count == 0
        assert row.stale is False
        assert row.status == "healthy"

    def test_record_success_for_unknown_job_is_noop(self, fake_service) -> None:
        service, _ = fake_service
        service.record_success("not_registered")
        assert service.snapshot() == []


class TestCronHealthServiceFailure:
    def test_record_failure_sanitizes_to_exception_class_only(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        try:
            raise ValueError("secret order_id=ORD-123 credential=sk-xxx")
        except ValueError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.last_failure_code == "ValueError"
        assert row.failure_count == 1
        # The raw message must never appear in the projection.
        assert "secret" not in (row.last_failure_code or "")
        assert "ORD-123" not in (row.last_failure_code or "")

    def test_record_failure_for_unknown_job_is_noop(self, fake_service) -> None:
        service, _ = fake_service
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("not_registered", exc)
        assert service.snapshot() == []

    def test_failure_with_no_exception_type_collapses_to_unknown(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)

        class _Empty(Exception):
            pass

        # Simulate an exception whose __name__ is empty (defensive).
        exc = _Empty("x")
        exc.__class__.__name__ = ""  # type: ignore[misc]
        service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.last_failure_code == "Unknown"


class TestCronHealthServiceStale:
    def test_enabled_job_within_interval_is_healthy(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.record_success("job")
        clock.advance(60.0)  # exactly one interval — still healthy (< 2x)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_enabled_job_past_stale_multiplier_is_stale(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.record_success("job")
        # Just past 2x interval (default multiplier 2.0).
        clock.advance(120.0 + 0.01)
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_enabled_job_just_below_stale_multiplier_not_stale(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.record_success("job")
        clock.advance(120.0 - 0.01)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_disabled_job_is_never_stale_even_if_overdue(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: False)
        service.record_success("job")
        clock.advance(1000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "disabled"
        assert row.enabled is False

    def test_enabled_unknown_provider_with_success_is_healthy(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=None)
        service.record_success("job")
        clock.advance(30.0)
        row = service.snapshot()[0]
        assert row.enabled is None
        assert row.stale is False
        assert row.status == "healthy"

    def test_enabled_unknown_provider_overdue_is_stale(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=None)
        service.record_success("job")
        clock.advance(200.0)
        row = service.snapshot()[0]
        assert row.enabled is None
        assert row.stale is True
        assert row.status == "stale"

    def test_never_succeeded_within_grace_period_is_pending(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        clock.advance(30.0)  # less than one interval
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "pending"

    def test_never_succeeded_past_grace_period_is_stale(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        clock.advance(200.0)  # past 2x interval since registration
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_no_interval_cannot_be_stale(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        service.record_success("job")
        clock.advance(10000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_no_interval_never_succeeded_is_unknown(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "unknown"

    def test_no_interval_only_failed_is_failing(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "failing"


class TestCronHealthServiceEnabledProvider:
    def test_enabled_provider_returning_none_reports_none(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: None)
        row = service.snapshot()[0]
        assert row.enabled is None

    def test_enabled_provider_raising_reports_none_and_does_not_break(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        row = service.snapshot()[0]
        assert row.enabled is None
        # Snapshot still works.
        assert row.name == "job"


class TestCronHealthServiceReset:
    def test_reset_clears_all_jobs(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)
        service.record_success("job")
        service.reset()
        assert service.snapshot() == []

    def test_isolation_between_services(self) -> None:
        clock1 = _FakeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
        clock2 = _FakeClock(datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc))
        s1 = CronHealthService(now=clock1)
        s2 = CronHealthService(now=clock2)
        s1.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        s1.record_success("job")
        # s2 is independent.
        assert s2.snapshot() == []
        assert len(s1.snapshot()) == 1


class TestCronHealthServiceMutatorSafety:
    def test_record_success_swallows_internal_errors(self, fake_service, monkeypatch: pytest.MonkeyPatch) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)

        def boom() -> datetime:
            raise RuntimeError("clock broken")

        # Replace the clock with one that raises; record_success must not raise.
        service._now = boom  # type: ignore[assignment]
        service.record_success("job")  # must not raise

    def test_record_failure_swallows_internal_errors(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)

        def boom() -> datetime:
            raise RuntimeError("clock broken")

        service._now = boom  # type: ignore[assignment]
        try:
            raise ValueError("x")
        except ValueError as exc:
            service.record_failure("job", exc)  # must not raise


class TestCronHealthAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.client = TestClient(app)

    def setup_method(self) -> None:
        settings.api_key = ""

    def test_endpoint_returns_typed_rows(self, fake_service) -> None:
        service, clock = fake_service
        service.register("llm_analysis", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.record_success("llm_analysis")
        clock.advance(10)
        resp = self.client.get("/api/cron-health")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert "as_of" in data
        assert isinstance(data["jobs"], list)
        row = data["jobs"][0]
        for key in (
            "name",
            "enabled",
            "expected_interval_seconds",
            "last_success_at",
            "last_failure_at",
            "last_failure_code",
            "tick_count",
            "failure_count",
            "stale",
            "status",
        ):
            assert key in row, f"missing field {key}"
        assert row["name"] == "llm_analysis"
        assert row["enabled"] is True
        assert row["tick_count"] == 1
        assert row["stale"] is False
        assert row["status"] == "healthy"

    def test_endpoint_does_not_leak_exception_messages(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        try:
            raise OSError("secret token=abc order_id=XYZ-9")
        except OSError as exc:
            service.record_failure("job", exc)
        body = self.client.get("/api/cron-health").text
        assert "secret" not in body
        assert "token=abc" not in body
        assert "XYZ-9" not in body
        assert "OSError" in body  # class name is safe to expose

    def test_endpoint_empty_when_no_jobs(self, fake_service) -> None:
        resp = self.client.get("/api/cron-health")
        assert resp.status_code == 200
        assert resp.json() == {"as_of": resp.json()["as_of"], "jobs": []}

    def test_auth_enforced_when_api_key_configured(self, fake_service, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(settings, "api_key", "ch-secret")
        assert self.client.get("/api/cron-health").status_code == 401
        resp = self.client.get("/api/cron-health", headers={"X-API-Key": "ch-secret"})
        assert resp.status_code == 200

    def test_as_of_matches_service_clock(self, fake_service) -> None:
        service, clock = fake_service
        service.register("job", expected_interval_seconds=60.0)
        resp = self.client.get("/api/cron-health")
        # as_of is the service clock value at call time (compare parsed values,
        # not the ISO string, since FastAPI may emit "Z" for UTC).
        as_of = datetime.fromisoformat(resp.json()["as_of"].replace("Z", "+00:00"))
        assert as_of == clock.now


class TestMainRegistersCronJobs:
    """Importing app.main registers all nine cron jobs with the shared service."""

    def test_all_expected_jobs_registered_on_import(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Use a fresh isolated service so we only see app.main's registrations.
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
        # Re-trigger the module-level registration by calling it directly.
        from app import main as main_module

        main_module._register_cron_health_jobs()
        names = {row.name for row in isolated.snapshot()}
        expected = {
            main_module._CRON_LLM_ANALYSIS,
            main_module._CRON_REPORT_SCHEDULE,
            main_module._CRON_ALERT_RULES,
            main_module._CRON_LLM_STORAGE_MAINTENANCE,
            main_module._CRON_STRATEGY_V2_SHADOW,
            main_module._CRON_OPENING_MOMENTUM_SHADOW,
            main_module._CRON_UNIVERSE_SELECTION,
            main_module._CRON_WATCHLIST_QUANT,
            main_module._CRON_WS_CLEANUP,
        }
        assert names == expected
        set_cron_health_service(None)

    def test_settings_gated_jobs_report_enabled_from_settings(self, monkeypatch: pytest.MonkeyPatch) -> None:
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
        from app import main as main_module

        monkeypatch.setattr(main_module.settings, "universe_selection_enabled", True)
        monkeypatch.setattr(main_module.settings, "watchlist_quant_auto_score_enabled", True)
        main_module._register_cron_health_jobs()
        rows = {row.name: row for row in isolated.snapshot()}
        assert rows[main_module._CRON_UNIVERSE_SELECTION].enabled is True
        assert rows[main_module._CRON_WATCHLIST_QUANT].enabled is True
        # Always-on jobs report True.
        assert rows[main_module._CRON_WS_CLEANUP].enabled is True
        assert rows[main_module._CRON_STRATEGY_V2_SHADOW].enabled is True
        # DB-gated jobs report None (no I/O in the health endpoint).
        assert rows[main_module._CRON_LLM_ANALYSIS].enabled is None
        assert rows[main_module._CRON_REPORT_SCHEDULE].enabled is None
        assert rows[main_module._CRON_ALERT_RULES].enabled is None
        set_cron_health_service(None)

    def test_disabled_settings_gated_jobs_report_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
        from app import main as main_module

        monkeypatch.setattr(main_module.settings, "universe_selection_enabled", False)
        monkeypatch.setattr(main_module.settings, "watchlist_quant_auto_score_enabled", False)
        main_module._register_cron_health_jobs()
        rows = {row.name: row for row in isolated.snapshot()}
        assert rows[main_module._CRON_UNIVERSE_SELECTION].enabled is False
        assert rows[main_module._CRON_WATCHLIST_QUANT].enabled is False
        set_cron_health_service(None)


class TestJobHealthSnapshotShape:
    def test_snapshot_rows_are_immutable_dataclass(self, fake_service) -> None:
        service, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        rows = service.snapshot()
        assert isinstance(rows[0], JobHealthSnapshot)
        with pytest.raises(Exception):
            rows[0].tick_count = 999  # type: ignore[misc]