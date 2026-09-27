"""Cron-health in-flight tick semantics — long ticks must not read stale.

Live defect (2026-09-27): ``strategy_v2_shadow`` sleeps 15s then runs a
~35s tick, so successes land every ~50s while the stale threshold is
2 × 15s = 30s measured from the last *completed* tick. Sampling every 4s
reported the healthy job ``stale`` in 20 of 45 live samples.

Contract under test:

* A tick that has started recently (``record_start``) is progress: the
  job is not stale while the in-flight tick is younger than the in-flight
  horizon (a multiple of the expected interval, default 4x — the measured
  production tick is ~2.3x the 15s sleep).
* The protection is bounded: a hung in-flight tick, a stopped loop, or a
  job with no success for long enough must still become stale.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from app import main as main_module
from app.services.cron_health_service import (
    CronHealthService,
    JobHealthSnapshot,
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


class TestInFlightTickNotStale:
    """Service-level semantics of the in-flight marker."""

    def _service(self) -> tuple[CronHealthService, _FakeMonotonicClock]:
        mono = _FakeMonotonicClock()
        wall = _FakeWallClock(datetime(2026, 9, 27, 5, 40, tzinfo=timezone.utc))
        service = CronHealthService(now_monotonic=mono, now_wall=wall)
        return service, mono

    def test_live_cadence_long_tick_never_stale_between_successes(self) -> None:
        """The observed production pattern: interval 15s, one success every
        ~50s (sleep 15 + tick 35), sampled in between — never stale."""
        service, mono = self._service()
        service.register(
            "strategy_v2_shadow",
            expected_interval_seconds=15.0,
            enabled_provider=lambda: True,
        )
        service.activate("strategy_v2_shadow")

        observed: list[tuple[float, bool, str]] = []
        for _cycle in range(3):
            mono.advance(15.0)  # loop sleep(15)
            service.record_start("strategy_v2_shadow")
            for _step in range(8):  # sample every ~4.4s during the ~35s tick
                mono.advance(4.375)
                row = service.snapshot()[0]
                observed.append((mono.t, row.stale, row.status))
            mono.advance(0.625)  # tick completes -> success every ~50s
            service.record_success("strategy_v2_shadow")

        assert observed, "the simulated loop must have produced samples"
        stale_samples = [entry for entry in observed if entry[1]]
        assert stale_samples == [], f"false stale samples: {stale_samples}"
        statuses = {entry[2] for entry in observed}
        assert statuses <= {"pending", "healthy"}

    def test_first_tick_hung_past_inflight_horizon_is_stale(self) -> None:
        """A first tick that starts but never completes must eventually be
        stale — the in-flight marker must not make staleness unbounded."""
        service, mono = self._service()
        service.register(
            "job", expected_interval_seconds=15.0, enabled_provider=lambda: True
        )
        service.activate("job")
        mono.advance(5.0)
        service.record_start("job")

        mono.advance(59.0)  # start age 59s <= 60s horizon -> still in flight
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "pending"

        mono.advance(1.01)  # start age 60.01s > 60s horizon -> hung tick
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_hung_tick_after_success_goes_stale(self) -> None:
        """Progress happened, then a tick started and hung: stale once the
        in-flight horizon lapses (not the plain 2x threshold)."""
        service, mono = self._service()
        service.register(
            "job", expected_interval_seconds=15.0, enabled_provider=lambda: True
        )
        service.activate("job")
        service.record_success("job")
        mono.advance(15.0)
        service.record_start("job")

        mono.advance(59.0)  # start age 59s -> protected
        row = service.snapshot()[0]
        assert row.stale is False
        assert row.status == "healthy"

        mono.advance(1.01)  # start age 60.01s > horizon; last tick 75.01s ago
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_stopped_loop_without_start_is_stale_as_before(self) -> None:
        """No in-flight marker: the pre-existing 2x-interval semantics must
        be unchanged (the fix may not relax detection for dead loops)."""
        service, mono = self._service()
        service.register(
            "job", expected_interval_seconds=15.0, enabled_provider=lambda: True
        )
        service.activate("job")
        service.record_success("job")

        mono.advance(30.01)
        row = service.snapshot()[0]
        assert row.stale is True
        assert row.status == "stale"

    def test_lapsed_start_protection_falls_back_to_last_tick_rule(self) -> None:
        """An old in-flight marker must not mask a long-dead loop: the plain
        last-tick rule applies once the start ages past the horizon."""
        service, mono = self._service()
        service.register(
            "job", expected_interval_seconds=15.0, enabled_provider=lambda: True
        )
        service.activate("job")
        service.record_success("job")  # t=1000
        mono.advance(15.0)
        service.record_start("job")  # t=1015, never finishes
        mono.advance(1000.0)  # far past both horizons
        row = service.snapshot()[0]
        assert row.stale is True

    def test_record_start_for_unknown_job_is_noop(self) -> None:
        service, _ = self._service()
        service.record_start("not_registered")
        assert service.snapshot() == []

    def test_record_start_swallows_internal_errors(self) -> None:
        service, _ = self._service()
        service.register("job", expected_interval_seconds=15.0)

        def boom() -> float:
            raise RuntimeError("clock broken")

        service._now_monotonic = boom  # type: ignore[assignment]
        service.record_start("job")  # must not raise


class TestStrategyV2ShadowCronWiring:
    """End-to-end: the real ``_strategy_v2_shadow_cron`` loop must not read
    stale while its (long) tick is executing. Drives the actual loop with a
    fake clock, a 35s fake tick, and mid-tick sampling — the live pattern."""

    @pytest.mark.asyncio
    async def test_long_tick_mid_samples_never_stale(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        mono = _FakeMonotonicClock()
        wall = _FakeWallClock(datetime(2026, 9, 27, 5, 40, tzinfo=timezone.utc))
        service = CronHealthService(now_monotonic=mono, now_wall=wall)
        set_cron_health_service(service)
        try:
            main_module._register_cron_health_jobs()
            main_module._activate_cron_health_jobs()

            samples: list[tuple[float, bool, str]] = []
            sleeps = {"n": 0}

            def _shadow_row() -> JobHealthSnapshot:
                return next(
                    row
                    for row in service.snapshot()
                    if row.name == main_module._CRON_STRATEGY_V2_SHADOW
                )

            async def fake_sleep(delay: float) -> None:
                sleeps["n"] += 1
                if sleeps["n"] > 3:
                    raise asyncio.CancelledError
                mono.advance(delay)
                wall.advance(delay)

            def fake_tick_sync() -> None:
                # A ~35s tick (measured production duration), sampled every
                # ~4.4s from inside the tick.
                for _step in range(8):
                    mono.advance(4.375)
                    wall.advance(4.375)
                    row = _shadow_row()
                    samples.append((mono.t, row.stale, row.status))
                mono.advance(0.625)
                wall.advance(0.625)

            monkeypatch.setattr(main_module.asyncio, "sleep", fake_sleep)
            monkeypatch.setattr(
                main_module, "_strategy_v2_shadow_tick_sync", fake_tick_sync
            )

            with pytest.raises(asyncio.CancelledError):
                await main_module._strategy_v2_shadow_cron()

            assert sleeps["n"] == 4  # three full cycles, cancel on the fourth
            assert len(samples) == 24
            stale_samples = [entry for entry in samples if entry[1]]
            assert stale_samples == [], f"false stale samples: {stale_samples}"
            row = _shadow_row()
            assert row.tick_count == 3
            assert row.last_outcome == "success"
            assert row.failure_count == 0
        finally:
            set_cron_health_service(None)
