"""Quote entitlement monitoring — observation-only matching, assessment, tick, API.

Covers: real-time package matching rules (US BBO markers, HK index-only
exclusion, cross-market non-matches), OK / EXPIRING / MISSING / UNKNOWN
assessment including the warn-day boundary (inclusive), capability-based
lapse detection (depth probe DENIED → MISSING overrides an OK listing;
PERMITTED → OK even with an absent listing; ERROR/SKIPPED keep the
package-based assessment), once-per-day-per-status notification dedupe with
severity mapping (EXPIRING=WARNING, MISSING=CRITICAL), UNKNOWN log-only
behavior, honest reason wording (never "day(s) left" as a fact for OK), the
pause/resume non-interaction proof via a recording _FakeRunner, the main.py
wiring seam, and the read-only endpoint shape including ``capability``.

The broker-side fetch (naive-UTC SDK datetimes -> tz-aware UTC QuotePackage)
and the depth permission probe live in tests/test_broker.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.broker import QuotePackage
from app.main import app
from app.services.quote_entitlement_service import (
    QuoteEntitlement,
    QuoteEntitlementService,
    assess_quote_entitlement,
    get_quote_entitlement_service,
    is_realtime_quote_package,
    set_quote_entitlement_service,
)

_NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)


def _pkg(
    key: str,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> QuotePackage:
    return QuotePackage(
        key=key,
        name=key,
        description=f"package {key}",
        start_at=start_at,
        end_at=end_at,
    )


# --- fakes ---------------------------------------------------------------


class _RecordingNotifier:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str | None]] = []

    def notify_risk_event(
        self,
        event_type: str,
        reason: str,
        *,
        severity: str | None = None,
    ) -> bool:
        self.calls.append((event_type, reason, severity))
        return True


class _FakeBroker:
    def __init__(
        self,
        packages: list[QuotePackage] | None = None,
        error: Exception | None = None,
        depth_probe: str = "PERMITTED",
    ) -> None:
        self.packages = packages or []
        self.error = error
        self.depth_probe = depth_probe
        self.calls = 0
        self.probe_calls: list[str] = []

    def get_quote_packages(self) -> list[QuotePackage]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return list(self.packages)

    def probe_depth_permission(self, symbol: str) -> str:
        self.probe_calls.append(symbol)
        if self.depth_probe == "ERROR":
            raise RuntimeError("probe transport failure")
        return self.depth_probe


_FORBIDDEN_PREFIXES = ("pause", "resume", "kill", "halt", "stop")


class _FakeRunner:
    """Records any access to lifecycle-control attributes (pause/resume/risk)."""

    def __init__(
        self,
        broker: _FakeBroker,
        *,
        market: str = "US",
        symbol: str = "NVDA.US",
    ) -> None:
        self.broker = broker
        self.engine = SimpleNamespace(
            params=SimpleNamespace(market=market, symbol=symbol)
        )
        self.notifier = _RecordingNotifier()
        self.forbidden_accesses: list[str] = []
        self.forbidden_calls: list[str] = []

    def __getattr__(self, name: str) -> Any:
        if name.startswith(_FORBIDDEN_PREFIXES) or name == "risk":
            self.forbidden_accesses.append(name)

            def _record(*_args: object, **_kwargs: object) -> None:
                self.forbidden_calls.append(name)

            return _record
        raise AttributeError(name)


# --- matching rules ------------------------------------------------------


class TestRealtimePackageMatching:
    def test_us_qbbo_key_matches_us(self) -> None:
        assert is_realtime_quote_package("US_QBBO_OpenAPI", market="US") is True

    def test_us_lv1_and_l1_keys_match_us(self) -> None:
        assert is_realtime_quote_package("US_LV1_AllPlatforms", market="US") is True
        assert is_realtime_quote_package("US_L1_AllPlatforms", market="US") is True

    def test_us_non_bbo_key_does_not_match_us(self) -> None:
        assert is_realtime_quote_package("US_SomeDerivativeFeed", market="US") is False

    def test_hk_index_package_is_excluded(self) -> None:
        assert (
            is_realtime_quote_package(
                "HK_HangSengIndex_AllTerminals", market="HK"
            )
            is False
        )
        assert is_realtime_quote_package("HK_Index_Only", market="HK") is False

    def test_hk_stock_package_matches_hk(self) -> None:
        assert (
            is_realtime_quote_package(
                "HK_L1_NonMainland_all_platforms", market="HK"
            )
            is True
        )

    def test_cn_key_is_not_treated_as_us(self) -> None:
        assert is_realtime_quote_package("CN_Connect", market="US") is False


# --- assessment ----------------------------------------------------------


class TestAssessQuoteEntitlement:
    def test_ok_when_active_and_far_from_expiry(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=20),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "OK"
        assert result.market == "US"
        assert result.package_key == "US_QBBO_OpenAPI"
        assert result.days_left == 20
        assert result.end_at == _NOW + timedelta(days=20)

    def test_expiring_inside_warn_window(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=5),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "EXPIRING"
        assert result.days_left == 5

    def test_warn_boundary_is_included_in_expiring(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=7),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "EXPIRING"
        assert result.days_left == 7

    def test_just_outside_warn_boundary_is_ok(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=7, seconds=1),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "OK"

    def test_missing_when_expired(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=400),
                end_at=_NOW - timedelta(days=1),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "MISSING"
        assert result.package_key == "US_QBBO_OpenAPI"
        assert result.days_left == -1
        assert result.reason

    def test_missing_when_not_yet_started(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW + timedelta(days=2),
                end_at=_NOW + timedelta(days=30),
            )
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "MISSING"
        assert result.package_key == "US_QBBO_OpenAPI"

    def test_missing_when_no_matching_package_at_all(self) -> None:
        packages = [_pkg("CN_Connect"), _pkg("HK_HangSengIndex_AllTerminals")]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "MISSING"
        assert result.package_key == ""
        assert result.end_at is None
        assert result.days_left is None

    def test_multiple_active_packages_use_latest_end_at(self) -> None:
        packages = [
            _pkg(
                "US_QBBO_OpenAPI",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=10),
            ),
            _pkg(
                "US_LV1_AllPlatforms",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=30),
            ),
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "OK"
        assert result.package_key == "US_LV1_AllPlatforms"
        assert result.days_left == 30

    def test_index_only_package_never_satisfies_hk(self) -> None:
        packages = [
            _pkg(
                "HK_HangSengIndex_AllTerminals",
                start_at=_NOW - timedelta(days=300),
                end_at=_NOW + timedelta(days=30),
            )
        ]
        result = assess_quote_entitlement(packages, market="HK", now=_NOW)
        assert result.status == "MISSING"

    def test_package_without_end_date_is_active_ok(self) -> None:
        packages = [
            _pkg("US_QBBO_OpenAPI", start_at=_NOW - timedelta(days=1), end_at=None)
        ]
        result = assess_quote_entitlement(packages, market="US", now=_NOW)
        assert result.status == "OK"
        assert result.end_at is None
        assert result.days_left is None


# --- service tick --------------------------------------------------------


class TestQuoteEntitlementServiceTick:
    def test_tick_assesses_and_caches_result(self) -> None:
        broker = _FakeBroker(
            packages=[
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=300),
                    end_at=_NOW + timedelta(days=20),
                )
            ]
        )
        runner = _FakeRunner(broker)
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        result = service.tick(_NOW)
        assert result.status == "OK"
        assert service.last_result() is result
        assert broker.calls == 1

    def test_tick_unknown_when_broker_raises(self) -> None:
        runner = _FakeRunner(_FakeBroker(error=OSError("socket closed")))
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        result = service.tick(_NOW)
        assert result.status == "UNKNOWN"
        assert "OSError" in result.reason
        assert service.last_result() is result

    def test_tick_ok_does_not_notify(self) -> None:
        runner = _FakeRunner(
            _FakeBroker(
                packages=[
                    _pkg(
                        "US_QBBO_OpenAPI",
                        start_at=_NOW - timedelta(days=300),
                        end_at=_NOW + timedelta(days=20),
                    )
                ]
            )
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        service.tick(_NOW)
        assert runner.notifier.calls == []

    def test_tick_notifies_expiring_warning_once_per_day(self) -> None:
        runner = _FakeRunner(
            _FakeBroker(
                packages=[
                    _pkg(
                        "US_QBBO_OpenAPI",
                        start_at=_NOW - timedelta(days=300),
                        end_at=_NOW + timedelta(days=3),
                    )
                ]
            )
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)

        first = service.tick(_NOW)
        assert first.status == "EXPIRING"
        assert len(runner.notifier.calls) == 1
        event_type, reason, severity = runner.notifier.calls[0]
        assert event_type == "QUOTE_ENTITLEMENT_EXPIRING"
        assert severity == "WARNING"
        assert "BBO" in reason
        assert "protective exits" in reason

        # Same UTC day: suppressed even though the tick re-runs.
        service.tick(_NOW + timedelta(hours=1))
        assert len(runner.notifier.calls) == 1

        # Next UTC day: notified again for the same status.
        service.tick(_NOW + timedelta(days=1, hours=1))
        assert len(runner.notifier.calls) == 2
        assert runner.notifier.calls[1][2] == "WARNING"

    def test_tick_notifies_missing_critical_once_per_day(self) -> None:
        # The authoritative lapse signal is the depth probe (DENIED); the
        # rolling-window listing alone can no longer manufacture MISSING.
        runner = _FakeRunner(
            _FakeBroker(
                packages=[
                    _pkg(
                        "US_QBBO_OpenAPI",
                        start_at=_NOW - timedelta(days=400),
                        end_at=_NOW - timedelta(days=2),
                    )
                ],
                depth_probe="DENIED",
            )
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)

        first = service.tick(_NOW)
        assert first.status == "MISSING"
        assert len(runner.notifier.calls) == 1
        event_type, reason, severity = runner.notifier.calls[0]
        assert event_type == "QUOTE_ENTITLEMENT_MISSING"
        assert severity == "CRITICAL"
        assert "BBO" in reason
        assert "protective exits" in reason

        service.tick(_NOW + timedelta(hours=2))
        assert len(runner.notifier.calls) == 1

    def test_tick_unknown_never_notifies_and_logs_throttled_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        runner = _FakeRunner(_FakeBroker(error=RuntimeError("quota")))
        service = QuoteEntitlementService(runner_factory=lambda: runner)

        with caplog.at_level("WARNING", logger="auto_trade.quote_entitlement"):
            first = service.tick(_NOW)
            service.tick(_NOW + timedelta(seconds=1))

        assert first.status == "UNKNOWN"
        assert runner.notifier.calls == []
        warnings = [
            r
            for r in caplog.records
            if r.levelname == "WARNING" and "UNKNOWN" in r.message
        ]
        assert len(warnings) == 1  # second occurrence suppressed by throttle

    def test_tick_never_calls_pause_resume_or_risk(self) -> None:
        for packages in (
            [  # OK
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=300),
                    end_at=_NOW + timedelta(days=20),
                )
            ],
            [  # MISSING
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=400),
                    end_at=_NOW - timedelta(days=1),
                )
            ],
        ):
            runner = _FakeRunner(_FakeBroker(packages=packages))
            service = QuoteEntitlementService(runner_factory=lambda: runner)
            service.tick(_NOW)
            assert runner.forbidden_accesses == []
            assert runner.forbidden_calls == []

        error_runner = _FakeRunner(_FakeBroker(error=OSError("down")))
        service = QuoteEntitlementService(runner_factory=lambda: error_runner)
        service.tick(_NOW)
        assert error_runner.forbidden_accesses == []
        assert error_runner.forbidden_calls == []

    def test_tick_reads_primary_market_from_runner_params(self) -> None:
        runner = _FakeRunner(
            _FakeBroker(
                packages=[
                    _pkg(
                        "HK_L1_NonMainland_all_platforms",
                        start_at=_NOW - timedelta(days=300),
                        end_at=_NOW + timedelta(days=20),
                    )
                ]
            ),
            market="HK",
            symbol="0700.HK",
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        result = service.tick(_NOW)
        assert result.market == "HK"
        assert result.status == "OK"
        assert result.package_key == "HK_L1_NonMainland_all_platforms"


class TestCapabilityProbePrecedence:
    """LongPort reports a rolling ~30-day window, so the package listing
    alone cannot detect a lapse. The depth capability probe on the primary
    symbol is the authoritative lapse signal."""

    def _service(
        self,
        *,
        packages: list[QuotePackage],
        depth_probe: str = "PERMITTED",
        symbol: str = "NVDA.US",
        market: str = "US",
    ) -> tuple[QuoteEntitlementService, _FakeRunner]:
        runner = _FakeRunner(
            _FakeBroker(packages=packages, depth_probe=depth_probe),
            market=market,
            symbol=symbol,
        )
        return (
            QuoteEntitlementService(runner_factory=lambda: runner),
            runner,
        )

    def test_denied_overrides_ok_listing_to_missing_critical(self) -> None:
        service, runner = self._service(
            packages=_us_packages(days=20), depth_probe="DENIED"
        )
        result = service.tick(_NOW)
        assert result.status == "MISSING"
        assert result.capability == "DENIED"
        assert result.reason == (
            "depth() refused for NVDA.US: no real-time quote "
            "permission (301604)"
        )
        assert len(runner.notifier.calls) == 1
        event_type, _reason, severity = runner.notifier.calls[0]
        assert event_type == "QUOTE_ENTITLEMENT_MISSING"
        assert severity == "CRITICAL"

        # Dedupe: same status on the same UTC day notifies once.
        service.tick(_NOW + timedelta(hours=1))
        assert len(runner.notifier.calls) == 1

    def test_permitted_with_absent_listing_gives_ok_no_notify(self) -> None:
        service, runner = self._service(
            packages=[],  # listing absent entirely
            depth_probe="PERMITTED",
        )
        result = service.tick(_NOW)
        assert result.status == "OK"
        assert result.capability == "PERMITTED"
        assert result.package_key == ""
        assert "depth" in result.reason.lower()
        assert runner.notifier.calls == []
        assert runner.broker.probe_calls == ["NVDA.US"]

    def test_permitted_keeps_expiring_fixed_window(self) -> None:
        service, runner = self._service(
            packages=_us_packages(days=3), depth_probe="PERMITTED"
        )
        result = service.tick(_NOW)
        assert result.status == "EXPIRING"
        assert result.capability == "PERMITTED"
        assert result.package_key == "US_QBBO_OpenAPI"
        assert len(runner.notifier.calls) == 1
        assert runner.notifier.calls[0][2] == "WARNING"

    def test_error_keeps_ok_listing_unchanged(self) -> None:
        service, runner = self._service(
            packages=_us_packages(days=20), depth_probe="ERROR"
        )
        result = service.tick(_NOW)
        assert result.status == "OK"
        assert result.capability == "ERROR"
        assert result.package_key == "US_QBBO_OpenAPI"
        assert runner.notifier.calls == []

    def test_empty_symbol_records_skipped(self) -> None:
        service, runner = self._service(
            packages=_us_packages(days=20), symbol=""
        )
        result = service.tick(_NOW)
        assert result.status == "OK"
        assert result.capability == "SKIPPED"
        assert runner.broker.probe_calls == []

    def test_denied_overrides_missing_listing_too(self) -> None:
        # Package fetch failed entirely (UNKNOWN) but the probe is DENIED:
        # the authoritative lapse signal still wins.
        service, runner = self._service(
            packages=[], depth_probe="DENIED"
        )
        result = service.tick(_NOW)
        assert result.status == "MISSING"
        assert result.capability == "DENIED"
        assert len(runner.notifier.calls) == 1
        assert runner.notifier.calls[0][2] == "CRITICAL"

    def test_unknown_from_fetch_error_keeps_capability_from_probe(
        self,
    ) -> None:
        runner = _FakeRunner(
            _FakeBroker(error=OSError("socket closed"), depth_probe="PERMITTED")
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        result = service.tick(_NOW)
        assert result.status == "UNKNOWN"
        assert result.capability == "PERMITTED"
        assert runner.notifier.calls == []

    def test_reason_never_says_days_left_for_ok(self) -> None:
        # Rolling window honesty: an OK verdict must never state "N day(s)
        # left" as a fact. Assert across every OK path.
        cases: list[QuoteEntitlement] = []
        for packages, depth_probe in (
            (_us_packages(days=20), "PERMITTED"),
            (_us_packages(days=20), "ERROR"),
            ([], "PERMITTED"),
        ):
            runner = _FakeRunner(
                _FakeBroker(packages=packages, depth_probe=depth_probe)
            )
            service = QuoteEntitlementService(runner_factory=lambda: runner)
            result = service.tick(_NOW)
            if result.status == "OK":
                cases.append(result)
        assert cases, "expected at least one OK case"
        for result in cases:
            assert result.status == "OK"
            assert "day(s) left" not in result.reason
            assert "days left" not in result.reason
            assert "expires" not in result.reason


def _us_packages(days: int) -> list[QuotePackage]:
    return [
        _pkg(
            "US_QBBO_OpenAPI",
            start_at=_NOW - timedelta(days=300),
            end_at=_NOW + timedelta(days=days),
        )
    ]


# --- main.py wiring seam ---------------------------------------------------


class TestMainWiring:
    def test_tick_sync_delegates_to_singleton_service(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module
        import app.services.quote_entitlement_service as service_module

        calls: list[datetime | None] = []

        class _RecordingService:
            def tick(self, now: datetime | None = None) -> None:
                calls.append(now)

        monkeypatch.setattr(
            service_module,
            "get_quote_entitlement_service",
            lambda: _RecordingService(),
        )
        main_module._quote_entitlement_tick_sync()
        assert len(calls) == 1
        assert calls[0] is not None
        assert calls[0].tzinfo is not None

    def test_cron_registered_with_cron_health(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import app.main as main_module
        from app.services.cron_health_service import (
            CronHealthService,
            set_cron_health_service,
        )

        isolated = CronHealthService()
        set_cron_health_service(isolated)
        try:
            main_module._register_cron_health_jobs()
            names = {row.name for row in isolated.snapshot()}
            assert main_module._CRON_QUOTE_ENTITLEMENT in names
            row = next(
                r
                for r in isolated.snapshot()
                if r.name == main_module._CRON_QUOTE_ENTITLEMENT
            )
            assert row.expected_interval_seconds == 6 * 3600.0
            assert row.enabled is True
        finally:
            set_cron_health_service(None)


# --- read-only API ---------------------------------------------------------


class TestQuoteEntitlementAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.client = TestClient(app)

    def setup_method(self) -> None:
        settings.api_key = ""

    def _service_with(
        self,
        packages: list[QuotePackage],
        *,
        depth_probe: str = "PERMITTED",
    ) -> QuoteEntitlementService:
        runner = _FakeRunner(
            _FakeBroker(packages=packages, depth_probe=depth_probe)
        )
        service = QuoteEntitlementService(runner_factory=lambda: runner)
        service.tick(_NOW)
        return service

    def test_endpoint_returns_cached_assessment_shape(self) -> None:
        service = self._service_with(
            [
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=300),
                    end_at=_NOW + timedelta(days=3),
                )
            ]
        )
        set_quote_entitlement_service(service)
        try:
            resp = self.client.get("/api/quote-entitlement")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert set(data.keys()) == {
                "market",
                "status",
                "package_key",
                "end_at",
                "days_left",
                "reason",
                "capability",
            }
            assert data["market"] == "US"
            assert data["status"] == "EXPIRING"
            assert data["package_key"] == "US_QBBO_OpenAPI"
            assert data["days_left"] == 3
            assert data["end_at"].endswith(("+00:00", "Z"))
            assert data["capability"] == "PERMITTED"
        finally:
            set_quote_entitlement_service(None)

    def test_endpoint_reports_denied_capability_as_missing(self) -> None:
        service = self._service_with(
            [
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=300),
                    end_at=_NOW + timedelta(days=28),
                )
            ],
            depth_probe="DENIED",
        )
        set_quote_entitlement_service(service)
        try:
            resp = self.client.get("/api/quote-entitlement")
            assert resp.status_code == 200, resp.text
            data = resp.json()
            assert data["status"] == "MISSING"
            assert data["capability"] == "DENIED"
            assert "301604" in data["reason"]
        finally:
            set_quote_entitlement_service(None)

    def test_endpoint_unavailable_before_first_assessment(self) -> None:
        set_quote_entitlement_service(QuoteEntitlementService())
        try:
            resp = self.client.get("/api/quote-entitlement")
            assert resp.status_code == 503
            data = resp.json()
            assert data["status"] == "UNKNOWN"
            assert data["capability"] == "SKIPPED"
            assert "not been assessed" in data["reason"]
        finally:
            set_quote_entitlement_service(None)

    def test_endpoint_enforces_api_key(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        service = self._service_with(
            [
                _pkg(
                    "US_QBBO_OpenAPI",
                    start_at=_NOW - timedelta(days=300),
                    end_at=_NOW + timedelta(days=20),
                )
            ]
        )
        set_quote_entitlement_service(service)
        try:
            monkeypatch.setattr(settings, "api_key", "qe-secret")
            assert (
                self.client.get("/api/quote-entitlement").status_code == 401
            )
            resp = self.client.get(
                "/api/quote-entitlement", headers={"X-API-Key": "qe-secret"}
            )
            assert resp.status_code == 200
        finally:
            set_quote_entitlement_service(None)

    def test_singleton_getter_returns_shared_instance(self) -> None:
        first = get_quote_entitlement_service()
        assert first is get_quote_entitlement_service()
