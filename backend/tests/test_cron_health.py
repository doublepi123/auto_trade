"""Cron job health service and API — process-local, fake-clock, read-only.

Covers: scheduler-loop health semantics (tick_count = attempts of either
outcome, latest-outcome controls health), success/failure recording,
reset/isolation, stale boundary (monotonic clock), disabled behavior,
activation/pending semantics, sanitized error projection, enabled-provider
failure isolation, concurrent snapshot/copy isolation, throwing
accessor/mutators, and the relevant main/startup registration/activation
behavior.
"""
from __future__ import annotations

import threading
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


class _FakeMonotonicClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeWallClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture()
def fake_service(monkeypatch: pytest.MonkeyPatch) -> Generator[
    tuple[CronHealthService, _FakeMonotonicClock, _FakeWallClock], None, None
]:
    mono = _FakeMonotonicClock()
    wall = _FakeWallClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
    service = CronHealthService(now_monotonic=mono, now_wall=wall)
    set_cron_health_service(service)
    monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: service)
    yield service, mono, wall
    set_cron_health_service(None)


class TestCronHealthServiceRegistration:
    def test_register_is_idempotent_preserving_counters(self, fake_service) -> None:
        service, _, _ = fake_service
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
        service, _, _ = fake_service
        service.register("zeta", expected_interval_seconds=10.0)
        service.register("alpha", expected_interval_seconds=10.0)
        names = [row.name for row in service.snapshot()]
        assert names == ["alpha", "zeta"]


class TestCronHealthServiceSuccess:
    def test_record_success_updates_last_success_and_tick_count(self, fake_service) -> None:
        service, mono, wall = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        mono.advance(5)
        wall.advance(5)
        service.record_success("job")
        mono.advance(10)
        wall.advance(10)
        row = service.snapshot()[0]
        assert row.tick_count == 1
        assert row.last_success_at == wall.now - timedelta(seconds=10)
        assert row.failure_count == 0
        assert row.last_outcome == "success"
        assert row.stale is False
        assert row.status == "healthy"

    def test_record_success_for_unknown_job_is_noop(self, fake_service) -> None:
        service, _, _ = fake_service
        service.record_success("not_registered")
        assert service.snapshot() == []


class TestCronHealthServiceFailure:
    def test_record_failure_sanitizes_to_exception_class_only(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        try:
            raise ValueError("secret order_id=ORD-123 credential=sk-xxx")
        except ValueError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.last_failure_code == "ValueError"
        assert row.failure_count == 1
        # tick_count counts attempts of either outcome.
        assert row.tick_count == 1
        assert row.last_outcome == "failure"
        # The raw message must never appear in the projection.
        assert "secret" not in (row.last_failure_code or "")
        assert "ORD-123" not in (row.last_failure_code or "")

    def test_record_failure_for_unknown_job_is_noop(self, fake_service) -> None:
        service, _, _ = fake_service
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("not_registered", exc)
        assert service.snapshot() == []

    def test_failure_with_no_exception_type_collapses_to_unknown(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)

        class _Empty(Exception):
            pass

        exc = _Empty("x")
        exc.__class__.__name__ = ""  # type: ignore[misc]
        service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.last_failure_code == "Unknown"


class TestCronHealthServiceLatestOutcome:
    """Latest-outcome semantics: no historical success masks the latest failure."""

    def test_first_failure_is_failing_not_pending(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.status == "failing"
        assert row.last_outcome == "failure"

    def test_success_then_failure_is_failing(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.status == "failing"
        assert row.last_outcome == "failure"
        assert row.tick_count == 2
        assert row.failure_count == 1

    def test_failure_then_success_is_healthy(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("job", exc)
        service.record_success("job")
        row = service.snapshot()[0]
        assert row.status == "healthy"
        assert row.last_outcome == "success"
        assert row.tick_count == 2
        assert row.failure_count == 1

    def test_disabled_noop_tick_is_counted_but_not_healthy(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: False)
        service.activate("job")
        # A disabled loop that completes a no-op tick is still a completed
        # tick, but the job is explicitly disabled.
        service.record_success("job")
        row = service.snapshot()[0]
        assert row.tick_count == 1
        assert row.enabled is False
        assert row.status == "disabled"
        assert row.stale is False


class TestCronHealthServiceStale:
    def test_enabled_job_within_interval_is_healthy(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        mono.advance(60.0)  # exactly one interval — still healthy (< 2x)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_enabled_job_past_stale_multiplier_is_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        mono.advance(120.0 + 0.01)
        row = service.snapshot()[0]
        assert row.stale is True
        # Latest outcome was success, but overdue -> stale.
        assert row.status == "stale"

    def test_enabled_job_just_below_stale_multiplier_not_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        mono.advance(120.0 - 0.01)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_disabled_job_is_never_stale_even_if_overdue(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: False)
        service.activate("job")
        service.record_success("job")
        mono.advance(1000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "disabled"
        assert row.enabled is False

    def test_enabled_unknown_provider_with_success_is_healthy(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=None)
        service.activate("job")
        service.record_success("job")
        mono.advance(30.0)
        row = service.snapshot()[0]
        assert row.enabled is None
        assert row.stale is False
        assert row.status == "healthy"

    def test_enabled_unknown_provider_overdue_is_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=None)
        service.activate("job")
        service.record_success("job")
        mono.advance(200.0)
        row = service.snapshot()[0]
        assert row.enabled is None
        assert row.stale is True
        assert row.status == "stale"

    def test_never_ticked_within_grace_period_is_pending(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        mono.advance(30.0)  # less than one interval
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "pending"

    def test_never_ticked_past_grace_period_is_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        mono.advance(200.0)  # past 2x interval since activation
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_not_activated_is_pending_never_stale(self, fake_service) -> None:
        """Delayed pre-start/import time: registered but not activated stays
        pending rather than stale, regardless of elapsed time."""
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        # Do NOT activate.
        mono.advance(10000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "pending"

    def test_no_interval_cannot_be_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        mono.advance(10000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_no_interval_never_succeeded_is_unknown(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        service.activate("job")
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "unknown"

    def test_no_interval_only_failed_is_failing(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=None, enabled_provider=lambda: True)
        service.activate("job")
        try:
            raise RuntimeError("boom")
        except RuntimeError as exc:
            service.record_failure("job", exc)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "failing"


class TestCronHealthServiceMonotonicVsWall:
    """Staleness uses monotonic clock; wall-clock jumps must not affect it."""

    def test_wall_clock_jump_does_not_affect_stale(self, fake_service) -> None:
        service, mono, wall = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        # Advance only the wall clock (simulating an NTP jump); monotonic
        # unchanged -> not stale.
        wall.advance(10000.0)
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

    def test_monotonic_advance_makes_stale(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        service.record_success("job")
        mono.advance(200.0)
        row = service.snapshot()[0]
        assert row.stale is True


class TestCronHealthServiceEnabledProvider:
    def test_enabled_provider_returning_none_reports_none(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: None)
        row = service.snapshot()[0]
        assert row.enabled is None

    def test_enabled_provider_raising_reports_none_and_does_not_break(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: (_ for _ in ()).throw(RuntimeError("db down")))
        row = service.snapshot()[0]
        assert row.enabled is None
        assert row.name == "job"


class TestCronHealthServiceReset:
    def test_reset_clears_all_jobs(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)
        service.record_success("job")
        service.reset()
        assert service.snapshot() == []

    def test_isolation_between_services(self) -> None:
        mono1 = _FakeMonotonicClock()
        wall1 = _FakeWallClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
        mono2 = _FakeMonotonicClock(start=5000.0)
        wall2 = _FakeWallClock(datetime(2027, 1, 1, 0, 0, tzinfo=timezone.utc))
        s1 = CronHealthService(now_monotonic=mono1, now_wall=wall1)
        s2 = CronHealthService(now_monotonic=mono2, now_wall=wall2)
        s1.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        s1.record_success("job")
        # s2 is independent.
        assert s2.snapshot() == []
        assert len(s1.snapshot()) == 1


class TestCronHealthServiceMutatorSafety:
    def test_record_success_swallows_internal_errors(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)

        def boom() -> float:
            raise RuntimeError("clock broken")

        service._now_monotonic = boom  # type: ignore[assignment]
        service.record_success("job")  # must not raise

    def test_record_failure_swallows_internal_errors(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)

        def boom() -> float:
            raise RuntimeError("clock broken")

        service._now_monotonic = boom  # type: ignore[assignment]
        try:
            raise ValueError("x")
        except ValueError as exc:
            service.record_failure("job", exc)  # must not raise

    def test_activate_swallows_internal_errors(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0)

        def boom() -> float:
            raise RuntimeError("clock broken")

        service._now_monotonic = boom  # type: ignore[assignment]
        service.activate("job")  # must not raise


class TestCronHealthServiceConcurrentSnapshot:
    """Concurrent snapshot/copy isolation: mutable state is copied under the
    lock, so concurrent ticks cannot tear the snapshot."""

    def test_concurrent_ticks_and_snapshots_are_consistent(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
        stop = threading.Event()
        errors: list[Exception] = []

        def ticker() -> None:
            try:
                while not stop.is_set():
                    service.record_success("job")
                    mono.advance(0.001)
            except Exception as exc:
                errors.append(exc)

        t = threading.Thread(target=ticker)
        t.start()
        try:
            for _ in range(200):
                rows = service.snapshot()
                for row in rows:
                    # tick_count and failure_count must be internally
                    # consistent: failure_count <= tick_count.
                    assert row.failure_count <= row.tick_count
        finally:
            stop.set()
            t.join(timeout=2)
        assert not errors


class TestCronHealthAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.client = TestClient(app)

    def setup_method(self) -> None:
        settings.api_key = ""

    def test_endpoint_returns_typed_rows(self, fake_service) -> None:
        service, mono, _ = fake_service
        service.register("llm_analysis", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("llm_analysis")
        service.record_success("llm_analysis")
        mono.advance(10)
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
            "last_outcome",
            "stale",
            "status",
        ):
            assert key in row, f"missing field {key}"
        assert row["name"] == "llm_analysis"
        assert row["enabled"] is True
        assert row["tick_count"] == 1
        assert row["last_outcome"] == "success"
        assert row["stale"] is False
        assert row["status"] == "healthy"

    def test_endpoint_does_not_leak_exception_messages(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        service.activate("job")
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

    def test_as_of_matches_service_wall_clock(self, fake_service) -> None:
        service, _, wall = fake_service
        service.register("job", expected_interval_seconds=60.0)
        resp = self.client.get("/api/cron-health")
        as_of = datetime.fromisoformat(resp.json()["as_of"].replace("Z", "+00:00"))
        assert as_of == wall.now


class TestMainRegistersCronJobs:
    """Registration/activation happens at lifespan startup, not import time."""

    def test_all_expected_jobs_registered_by_register_call(self, monkeypatch: pytest.MonkeyPatch) -> None:
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
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
            main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION,
            main_module._CRON_WS_CLEANUP,
        }
        assert names == expected
        set_cron_health_service(None)

    def test_jobs_pending_until_activated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
        from app import main as main_module

        main_module._register_cron_health_jobs()
        # Before activation, jobs are pending or disabled (if their settings
        # gate is off), but never stale.
        for row in isolated.snapshot():
            assert row.status in ("pending", "disabled")
            assert row.stale is False
        # After activation, enabled jobs are still pending (no ticks yet,
        # within grace); disabled jobs remain disabled.
        main_module._activate_cron_health_jobs()
        for row in isolated.snapshot():
            assert row.status in ("pending", "disabled")
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
        assert rows[main_module._CRON_WS_CLEANUP].enabled is True
        assert rows[main_module._CRON_STRATEGY_V2_SHADOW].enabled is True
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
        monkeypatch.setattr(
            main_module.settings, "watchlist_quant_v6_evaluation_enabled", False
        )
        main_module._register_cron_health_jobs()
        rows = {row.name: row for row in isolated.snapshot()}
        assert rows[main_module._CRON_UNIVERSE_SELECTION].enabled is False
        assert rows[main_module._CRON_WATCHLIST_QUANT].enabled is False
        assert rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION].enabled is False
        # Disabled jobs are not stale and do not pretend enabled work ran.
        assert rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION].stale is False
        assert rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION].status == "disabled"
        set_cron_health_service(None)

    def test_quant_v6_enabled_reports_true_and_expected_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolated = CronHealthService()
        set_cron_health_service(isolated)
        monkeypatch.setattr(cron_health_api, "get_cron_health_service", lambda: isolated)
        from app import main as main_module

        monkeypatch.setattr(
            main_module.settings, "watchlist_quant_v6_evaluation_enabled", True
        )
        monkeypatch.setattr(
            main_module.settings, "watchlist_quant_v6_evaluation_interval_minutes", 180
        )
        main_module._register_cron_health_jobs()
        rows = {row.name: row for row in isolated.snapshot()}
        v6 = rows[main_module._CRON_WATCHLIST_QUANT_V6_EVALUATION]
        assert v6.enabled is True
        assert v6.expected_interval_seconds == 180 * 60
        set_cron_health_service(None)


class TestRepeatedLifecycleIsolation:
    """Replacing the singleton restores the prior instance; repeated app/
    TestClient lifecycles cannot leave later registries empty or contaminated."""

    def test_repeated_set_and_reset_isolation(self) -> None:
        # First lifecycle.
        s1 = CronHealthService()
        set_cron_health_service(s1)
        from app import main as main_module

        main_module._register_cron_health_jobs()
        assert len(s1.snapshot()) == 10
        # Reset to None (simulating teardown).
        set_cron_health_service(None)
        # Second lifecycle with a fresh service.
        s2 = CronHealthService()
        set_cron_health_service(s2)
        main_module._register_cron_health_jobs()
        # s2 must have exactly 10 jobs (not contaminated by s1, not empty).
        assert len(s2.snapshot()) == 10
        names = {row.name for row in s2.snapshot()}
        assert names == {row.name for row in s1.snapshot()}
        set_cron_health_service(None)


class TestJobHealthSnapshotShape:
    def test_snapshot_rows_are_immutable_dataclass(self, fake_service) -> None:
        service, _, _ = fake_service
        service.register("job", expected_interval_seconds=60.0, enabled_provider=lambda: True)
        rows = service.snapshot()
        assert isinstance(rows[0], JobHealthSnapshot)
        with pytest.raises(Exception):
            rows[0].tick_count = 999  # type: ignore[misc]