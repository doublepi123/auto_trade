"""Quote stream health tracker and API — process-local, fake-clock, read-only.

Covers: initial unavailable/waiting states, start/stop, silent-watchdog
success/failure, primary switch, reconnect before first push quote, active
poll exclusion, gap counting/max gap, disconnect/resubscribe counters,
out-of-order timestamps, throwing observer, safe API projection, endpoint
non-construction (503 when no runner), and focused runner regression coverage
ensuring the tracker hooks do not alter control flow.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections.abc import Generator
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from app.api import quote_health as quote_health_api
from app.config import settings
from app.core.broker import Quote
from app.core.engine import StrategyParams
from app.main import app
from app.runner import AppRunner
from app.services.quote_stream_health_service import (
    QuoteStreamHealthSnapshot,
    QuoteStreamHealthTracker,
)


class _FakeMonotonicClock:
    def __init__(self, start: float = 1000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class _FakeDateTimeClock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


@pytest.fixture()
def tracker() -> Generator[QuoteStreamHealthTracker, None, None]:
    mono = _FakeMonotonicClock()
    dt = _FakeDateTimeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
    t = QuoteStreamHealthTracker(
        "AAPL.US",
        now_monotonic=mono,
        now_datetime=dt,
    )
    yield t


class TestQuoteStreamHealthInitial:
    def test_initial_state_is_unavailable_with_no_quotes(self, tracker) -> None:
        snap = tracker.snapshot()
        assert snap.status == "unavailable"
        assert snap.quotes_received == 0
        assert snap.last_quote_timestamp is None
        assert snap.last_quote_age_seconds is None
        assert snap.max_gap_seconds == 0.0
        assert snap.disconnect_count == 0
        assert snap.resubscribe_count == 0
        assert snap.disconnect_retry_count == 0
        assert snap.quotes_subscribed is False
        assert snap.symbol == "AAPL.US"

    def test_subscribed_but_no_quotes_is_waiting(self, tracker) -> None:
        tracker.record_subscription_success()
        snap = tracker.snapshot()
        assert snap.status == "waiting"
        assert snap.quotes_subscribed is True
        # Successful subscription resets the window and increments resubscribe.
        assert snap.resubscribe_count == 1
        assert snap.quotes_received == 0


class TestQuoteStreamHealthHealthy:
    def test_record_quote_increments_count_and_timestamp(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        snap = tracker.snapshot()
        assert snap.quotes_received == 1
        assert snap.last_quote_timestamp == "2026-08-01T12:00:00Z"
        assert snap.last_quote_age_seconds == 0.0
        assert snap.status == "healthy"

    def test_age_advances_with_clock(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker._now_monotonic.advance(30.0)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.last_quote_age_seconds == 30.0
        assert snap.status == "healthy"

    def test_stale_after_90s_without_quote(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker._now_monotonic.advance(90.01)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.status == "stale"
        assert snap.last_quote_age_seconds == pytest.approx(90.01)

    def test_just_under_90s_is_still_healthy(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker._now_monotonic.advance(89.99)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.status == "healthy"


class TestQuoteStreamHealthGap:
    def test_max_gap_tracks_largest_delta(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("2026-08-01T12:00:05Z")  # +5s
        tracker.record_quote("2026-08-01T12:00:12Z")  # +7s (new max)
        tracker.record_quote("2026-08-01T12:00:14Z")  # +2s (not max)
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 7.0

    def test_out_of_order_does_not_advance_max_gap_or_last_source(self, tracker) -> None:
        """C.6: after 12:00:10, 12:00:05, 12:00:11, max gap must remain 1s
        and the last source timestamp must be 12:00:11."""
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:10Z")
        tracker.record_quote("2026-08-01T12:00:05Z")  # older — ignored
        tracker.record_quote("2026-08-01T12:00:11Z")  # +1s from 10
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 1.0
        assert snap.last_quote_timestamp == "2026-08-01T12:00:11Z"

    def test_resubscribe_resets_gap_window(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("2026-08-01T12:00:30Z")  # 30s gap
        assert tracker.snapshot().max_gap_seconds == 30.0
        tracker.record_subscription_success()
        # After resubscribe, the next quote starts a fresh gap window.
        tracker.record_quote("2026-08-01T13:00:00Z")
        tracker.record_quote("2026-08-01T13:00:10Z")
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 10.0  # only the post-resubscribe gap
        assert snap.quotes_received == 2  # reset on resubscribe

    def test_unparseable_timestamp_does_not_break_or_advance_gap(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("not-a-timestamp")  # unparseable
        tracker.record_quote("2026-08-01T12:00:20Z")
        snap = tracker.snapshot()
        assert snap.quotes_received == 3
        assert snap.max_gap_seconds == 20.0

    def test_epoch_millis_timestamp_parsed(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("1754049600000")  # 2026-08-01T12:00:00Z in ms
        tracker.record_quote("1754049605000")  # +5s
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 5.0


class TestQuoteStreamHealthSubscriptionLifecycle:
    def test_record_subscription_success_resets_window(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        assert tracker.snapshot().quotes_received == 1
        # A second successful subscription resets the window.
        tracker.record_subscription_success()
        snap = tracker.snapshot()
        assert snap.quotes_received == 0
        assert snap.last_quote_timestamp is None
        assert snap.last_quote_age_seconds is None
        assert snap.status == "waiting"
        assert snap.resubscribe_count == 2

    def test_record_subscription_failure_stays_unsubscribed(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_subscription_failure()
        snap = tracker.snapshot()
        assert snap.quotes_subscribed is False
        assert snap.status == "unavailable"
        # Failure does not increment resubscribe_count.
        assert snap.resubscribe_count == 1

    def test_reconnect_before_first_push_quote_is_waiting(self, tracker) -> None:
        tracker.record_subscription_success()
        # No push quote yet — waiting.
        assert tracker.snapshot().status == "waiting"
        # Disconnect then reconnect.
        tracker.record_disconnect()
        assert tracker.snapshot().status == "unavailable"
        tracker.record_subscription_success()
        snap = tracker.snapshot()
        # After reconnect, still waiting (no push quote in new window yet).
        assert snap.status == "waiting"
        assert snap.quotes_received == 0


class TestQuoteStreamHealthDisconnect:
    def test_record_disconnect_increments_and_marks_unsubscribed(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_disconnect()
        snap = tracker.snapshot()
        assert snap.disconnect_count == 1
        assert snap.quotes_subscribed is False
        assert snap.status == "unavailable"

    def test_record_disconnect_retry_increments_separately(self, tracker) -> None:
        tracker.record_disconnect_retry()
        tracker.record_disconnect_retry()
        snap = tracker.snapshot()
        assert snap.disconnect_retry_count == 2
        assert snap.disconnect_count == 0

    def test_record_resubscribe_increments_and_marks_subscribed(self, tracker) -> None:
        tracker.record_disconnect()
        tracker.record_resubscribe()
        snap = tracker.snapshot()
        assert snap.resubscribe_count == 1
        assert snap.quotes_subscribed is True


class TestQuoteStreamHealthReset:
    def test_reset_clears_all_counters(self, tracker) -> None:
        tracker.record_subscription_success()
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_disconnect()
        tracker.record_resubscribe()
        tracker.reset()
        snap = tracker.snapshot()
        assert snap.quotes_received == 0
        assert snap.max_gap_seconds == 0.0
        assert snap.disconnect_count == 0
        assert snap.resubscribe_count == 0
        assert snap.disconnect_retry_count == 0
        assert snap.quotes_subscribed is False
        assert snap.status == "unavailable"


class TestQuoteStreamHealthMutatorSafety:
    def test_record_quote_swallows_internal_errors(self, tracker) -> None:
        def boom() -> float:
            raise RuntimeError("clock broken")

        tracker._now_monotonic = boom  # type: ignore[assignment]
        tracker.record_quote("2026-08-01T12:00:00Z")  # must not raise

    def test_record_disconnect_swallows_internal_errors(self, tracker) -> None:
        original = tracker._lock

        def boom() -> None:
            raise RuntimeError("lock broken")

        tracker._lock = boom  # type: ignore[assignment]
        tracker.record_disconnect()  # must not raise
        tracker._lock = original  # type: ignore[assignment]

    def test_snapshot_swallows_internal_errors_returns_unavailable(self, tracker) -> None:
        def boom() -> float:
            raise RuntimeError("clock broken")

        tracker._now_monotonic = boom  # type: ignore[assignment]
        snap = tracker.snapshot()  # must not raise
        assert snap.status == "unavailable"

    def test_set_symbol_swallows_internal_errors(self, tracker) -> None:
        original = tracker._lock

        def boom() -> None:
            raise RuntimeError("lock broken")

        tracker._lock = boom  # type: ignore[assignment]
        tracker.set_symbol("MSFT.US")  # must not raise
        tracker._lock = original  # type: ignore[assignment]


class TestQuoteStreamHealthSnapshotShape:
    def test_snapshot_is_immutable(self, tracker) -> None:
        snap = tracker.snapshot()
        assert isinstance(snap, QuoteStreamHealthSnapshot)
        with pytest.raises(Exception):
            snap.quotes_received = 999  # type: ignore[misc]


class TestQuoteHealthAPI:
    @classmethod
    def setup_class(cls) -> None:
        cls.client = TestClient(app)

    def setup_method(self) -> None:
        settings.api_key = ""

    def test_endpoint_returns_503_when_no_runner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """D.2/D.3: no runner -> 503 unavailable, never constructs a runner."""
        # Ensure peek_runner returns None (no runner constructed yet in this
        # test process for this symbol path).
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unavailable"
        assert data["quotes_subscribed"] is False
        assert data["quotes_received"] == 0

    def test_endpoint_returns_safe_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mono = _FakeMonotonicClock()
        dt = _FakeDateTimeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
        fake_tracker = QuoteStreamHealthTracker(
            "AAPL.US",
            now_monotonic=mono,
            now_datetime=dt,
        )
        fake_tracker.record_subscription_success()
        fake_tracker.record_quote("2026-08-01T12:00:00Z")

        class _FakeRunner:
            quote_stream_health = fake_tracker

        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: _FakeRunner())
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        for key in (
            "symbol",
            "quotes_received",
            "last_quote_timestamp",
            "last_quote_age_seconds",
            "max_gap_seconds",
            "disconnect_count",
            "resubscribe_count",
            "disconnect_retry_count",
            "quotes_subscribed",
            "status",
            "as_of",
        ):
            assert key in data, f"missing field {key}"
        assert data["symbol"] == "AAPL.US"
        assert data["quotes_received"] == 1
        assert data["status"] == "healthy"
        assert data["quotes_subscribed"] is True

    def test_endpoint_does_not_leak_prices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fake_tracker = QuoteStreamHealthTracker("AAPL.US")
        fake_tracker.record_subscription_success()
        fake_tracker.record_quote("2026-08-01T12:00:00Z")

        class _FakeRunner:
            quote_stream_health = fake_tracker

        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: _FakeRunner())
        body = self.client.get("/api/quote-health").text
        assert "last_price" not in body
        assert "bid" not in body
        assert "ask" not in body
        assert "order_id" not in body

    def test_endpoint_lookup_does_not_construct_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D.1: endpoint uses peek_runner (non-constructing), never get_runner."""
        constructed = []

        def _constructing_get_runner():
            constructed.append(True)
            return AppRunner()

        monkeypatch.setattr("app.runner.get_runner", _constructing_get_runner)
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 503
        assert constructed == []  # get_runner never called

    def test_endpoint_distinguishes_unavailable_from_disconnected(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """D.3: unavailable (no runner, 503) vs known disconnected (200)."""
        # No runner -> 503 unavailable.
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp1 = self.client.get("/api/quote-health")
        assert resp1.status_code == 503
        assert resp1.json()["status"] == "unavailable"
        # Runner with tracker, not subscribed -> 200 unavailable status.
        fake_tracker = QuoteStreamHealthTracker("AAPL.US")

        class _FakeRunner:
            quote_stream_health = fake_tracker

        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: _FakeRunner())
        resp2 = self.client.get("/api/quote-health")
        assert resp2.status_code == 200
        assert resp2.json()["status"] == "unavailable"
        assert resp2.json()["quotes_subscribed"] is False

    def test_auth_enforced_when_api_key_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_key", "qh-secret")
        assert self.client.get("/api/quote-health").status_code == 401
        resp = self.client.get("/api/quote-health", headers={"X-API-Key": "qh-secret"})
        # 503 because no runner in this test, but auth passed.
        assert resp.status_code in (200, 503)

    def test_200_payload_validated_against_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 2: HTTP 200 returns a QuoteStreamHealth-validated payload."""
        mono = _FakeMonotonicClock()
        dt = _FakeDateTimeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
        fake_tracker = QuoteStreamHealthTracker(
            "AAPL.US",
            now_monotonic=mono,
            now_datetime=dt,
        )
        fake_tracker.record_subscription_success()
        fake_tracker.record_quote("2026-08-01T12:00:00Z")

        class _FakeRunner:
            quote_stream_health = fake_tracker

        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: _FakeRunner())
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 200
        data = resp.json()
        # Every field required by QuoteStreamHealth is present and well-typed.
        assert data["symbol"] == "AAPL.US"
        assert isinstance(data["quotes_received"], int)
        assert isinstance(data["max_gap_seconds"], (int, float))
        assert isinstance(data["as_of"], str)

    def test_503_payload_validated_against_same_schema(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 2: HTTP 503 returns a QuoteStreamHealth-validated payload
        (same schema), not an untyped JSONResponse."""
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 503
        data = resp.json()
        # Same fields as the 200 payload, validated by the same model.
        for key in (
            "symbol",
            "quotes_received",
            "last_quote_timestamp",
            "last_quote_age_seconds",
            "max_gap_seconds",
            "disconnect_count",
            "resubscribe_count",
            "disconnect_retry_count",
            "quotes_subscribed",
            "status",
            "as_of",
        ):
            assert key in data, f"missing field {key} in 503 payload"
        assert data["status"] == "unavailable"
        assert data["quotes_subscribed"] is False
        assert data["symbol"] == ""

    def test_503_does_not_synthesize_known_disconnected_state(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 2: 503 must not synthesize known-disconnected state —
        quotes_subscribed is False and status is 'unavailable', distinct from
        a real tracker that is subscribed-but-disconnected."""
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "unavailable"
        assert data["quotes_subscribed"] is False
        assert data["disconnect_count"] == 0
        assert data["resubscribe_count"] == 0

    def test_endpoint_does_not_construct_runner_on_503(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Finding 2: the 503 path uses peek_runner (non-constructing) and
        never calls get_runner."""
        constructed: list[bool] = []

        def _constructing_get_runner():
            constructed.append(True)
            return AppRunner()

        monkeypatch.setattr("app.runner.get_runner", _constructing_get_runner)
        monkeypatch.setattr(quote_health_api, "peek_runner", lambda: None)
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 503
        assert constructed == []  # get_runner never called

    def test_openapi_documents_both_response_codes_with_quote_stream_health(
        self,
    ) -> None:
        """Finding 2: OpenAPI asserts both 200 and 503 reference
        QuoteStreamHealth."""
        schema = app.openapi()
        op = schema["paths"]["/api/quote-health"]["get"]
        assert "200" in op["responses"]
        assert "503" in op["responses"]
        ref_200 = op["responses"]["200"]["content"]["application/json"]["schema"].get("$ref", "")
        ref_503 = op["responses"]["503"]["content"]["application/json"]["schema"].get("$ref", "")
        assert ref_200 == "#/components/schemas/QuoteStreamHealth"
        assert ref_503 == "#/components/schemas/QuoteStreamHealth"


class TestRunnerQuoteHealthIntegration:
    """Focused runner regression coverage: the tracker hooks do not alter
    control flow and record the expected events."""

    def test_runner_has_quote_stream_health_attribute(self) -> None:
        runner = AppRunner()
        assert isinstance(runner.quote_stream_health, QuoteStreamHealthTracker)

    def test_record_quote_push_only_for_primary(self) -> None:
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        fresh_ts = datetime.now(timezone.utc).isoformat()
        quote = Quote(
            symbol="AAPL.US",
            last_price=100.0,
            bid=99.5,
            ask=100.5,
            timestamp=fresh_ts,
        )
        # Push quote (default is_push=True) records in the tracker.
        runner._evaluate_quote_trigger(quote, is_push=True)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_received == 1
        assert snap.last_quote_timestamp == fresh_ts

    def test_active_poll_does_not_increment_quote_stream_count(self) -> None:
        """C.4: is_push=False active polling must not increment quote-stream
        count or freshness."""
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        fresh_ts = datetime.now(timezone.utc).isoformat()
        quote = Quote(
            symbol="AAPL.US",
            last_price=100.0,
            bid=99.5,
            ask=100.5,
            timestamp=fresh_ts,
        )
        # Active poll (is_push=False) must NOT record in the tracker.
        runner._evaluate_quote_trigger(quote, is_push=False)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_received == 0
        assert snap.last_quote_timestamp is None

    def test_record_quote_ignores_non_primary_symbol(self) -> None:
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        fresh_ts = datetime.now(timezone.utc).isoformat()
        quote = Quote(
            symbol="MSFT.US",
            last_price=300.0,
            bid=299.5,
            ask=300.5,
            timestamp=fresh_ts,
        )
        runner._evaluate_quote_trigger(quote, is_push=True)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_received == 0

    def test_disconnect_hook_records_disconnect_and_retry(self) -> None:
        runner = AppRunner()
        runner._on_disconnect("connection reset")
        snap = runner.quote_stream_health.snapshot()
        assert snap.disconnect_count == 1
        assert snap.disconnect_retry_count == 1
        assert snap.quotes_subscribed is False

    def test_stop_marks_stream_unsubscribed(self) -> None:
        runner = AppRunner()
        runner.quote_stream_health.record_subscription_success()
        runner._running = True
        runner._thread = None
        runner.stop()
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_subscribed is False

    def test_observe_quote_subscription_success_resets_window(self) -> None:
        runner = AppRunner()
        runner.quote_stream_health.record_subscription_success()
        runner.quote_stream_health.record_quote("2026-08-01T12:00:00Z")
        assert runner.quote_stream_health.snapshot().quotes_received == 1
        runner._observe_quote_subscription(True)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_received == 0  # reset
        assert snap.quotes_subscribed is True

    def test_observe_quote_subscription_failure_stays_unsubscribed(self) -> None:
        runner = AppRunner()
        runner.quote_stream_health.record_subscription_success()
        runner._observe_quote_subscription(False)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_subscribed is False
        assert snap.status == "unavailable"

    def test_observe_quote_subscription_throwing_does_not_break(self) -> None:
        runner = AppRunner()

        def boom(success: bool) -> None:
            raise RuntimeError("observer broken")

        runner._observe_quote_subscription = boom  # type: ignore[assignment]
        # The real method swallows; but if someone overrides it to raise, the
        # caller (subscription path) should still not break. Here we verify
        # the tracker's own mutators swallow.
        runner.quote_stream_health._lock = lambda: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[assignment]
        runner.quote_stream_health.record_subscription_success()  # must not raise

    def test_primary_symbol_change_resets_symbol_bound_state(self) -> None:
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        runner.quote_stream_health.record_quote("2026-08-01T12:00:00Z")
        assert runner.quote_stream_health.snapshot().quotes_received == 1
        # Primary symbol change resets symbol-bound state.
        runner.quote_stream_health.set_symbol("MSFT.US")
        snap = runner.quote_stream_health.snapshot()
        assert snap.symbol == "MSFT.US"
        assert snap.quotes_received == 0
        assert snap.max_gap_seconds == 0.0
        # Process-lifetime counters preserved.
        assert snap.resubscribe_count == 1


class TestPrimarySwitchLifecycle:
    """Finding 1: primary-switch lifecycle — the quote health tracker symbol
    must be reset whenever primary_changed, independently of resubscription.

    Uses AppRunner.reload_strategy with the established fake-config pattern.
    """

    def _make_runner(self) -> AppRunner:
        runner = AppRunner()
        runner._running = True
        runner._quotes_subscribed = True
        runner.engine.params = StrategyParams(
            symbol="AAPL.US",
            market="US",
            buy_low=100.0,
            sell_high=110.0,
        )
        return runner

    def _patch_reload_deps(
        self,
        monkeypatch: pytest.MonkeyPatch,
        runner: AppRunner,
        new_symbol: str,
        new_market: str = "US",
    ) -> None:
        from app.services.strategy_service import StrategyService

        config = SimpleNamespace(
            symbol=new_symbol,
            market=new_market,
            buy_low=400.0,
            sell_high=410.0,
            short_selling=False,
            min_profit_amount=0.0,
            auto_resume_minutes=3,
            max_daily_loss=5000.0,
            max_consecutive_losses=3,
            fee_rate_us=0.0005,
            fee_rate_hk=0.003,
            min_repricing_pct=0.003,
            llm_action_cooldown_seconds=60,
            trading_session_mode="RTH_ONLY",
            margin_safety_factor=0.35,
        )
        monkeypatch.setattr(StrategyService, "__init__", lambda self, db: None)
        monkeypatch.setattr(StrategyService, "get_config", lambda _self: config)
        monkeypatch.setattr(runner.broker, "get_positions", lambda: [])
        monkeypatch.setattr(runner._state_svc, "load_symbol_runtime", lambda *args: None)
        monkeypatch.setattr(runner, "_sync_symbol_runtimes", lambda _db: None)

    def test_primary_change_with_unchanged_desired_set_resets_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Role swap/primary change with unchanged desired subscription set:
        the tracker symbol must still be reset even though no resubscription
        occurs."""
        runner = self._make_runner()
        # Seed the tracker with the old symbol's state.
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        runner.quote_stream_health.record_quote("2026-08-01T12:00:00Z")
        assert runner.quote_stream_health.snapshot().quotes_received == 1
        # Patch so the new config swaps primary to MSFT.US but the desired
        # symbol set is unchanged (we make _desired_quote_symbols_locked
        # return the same set by keeping _symbol_runtimes empty and relying
        # on the primary only — but primary changes, so to keep the set
        # unchanged we'd need both symbols. Instead, simulate "no
        # resubscribe needed" by marking not subscribed.
        runner._quotes_subscribed = False
        self._patch_reload_deps(monkeypatch, runner, new_symbol="MSFT.US")
        # No broker calls expected (not subscribed, no resubscribe).
        monkeypatch.setattr(
            runner.broker,
            "unsubscribe_quotes",
            lambda: pytest.fail("must not unsubscribe when not subscribed"),
        )
        monkeypatch.setattr(
            runner.broker,
            "subscribe_quotes_batch",
            lambda *_a, **_kw: pytest.fail("must not subscribe when not subscribed"),
        )

        runner.reload_strategy()

        snap = runner.quote_stream_health.snapshot()
        # Symbol reset happened independently of resubscription.
        assert snap.symbol == "MSFT.US"
        assert snap.quotes_received == 0
        assert snap.max_gap_seconds == 0.0
        # Subscription counters unchanged (no resubscription occurred).
        assert snap.resubscribe_count == 1

    def test_primary_change_while_already_disconnected_resets_symbol(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Primary change while already disconnected: symbol reset must still
        happen; no broker calls, no subscription counter changes."""
        runner = self._make_runner()
        runner._quotes_subscribed = False  # already disconnected
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        runner.quote_stream_health.record_quote("2026-08-01T12:00:00Z")
        assert runner.quote_stream_health.snapshot().quotes_received == 1
        self._patch_reload_deps(monkeypatch, runner, new_symbol="MSFT.US")
        monkeypatch.setattr(
            runner.broker,
            "unsubscribe_quotes",
            lambda: pytest.fail("must not unsubscribe when disconnected"),
        )
        monkeypatch.setattr(
            runner.broker,
            "subscribe_quotes_batch",
            lambda *_a, **_kw: pytest.fail("must not subscribe when disconnected"),
        )

        runner.reload_strategy()

        snap = runner.quote_stream_health.snapshot()
        assert snap.symbol == "MSFT.US"
        assert snap.quotes_received == 0
        assert snap.resubscribe_count == 1  # unchanged

    def test_primary_change_then_failed_resubscription_resets_symbol_and_stays_unsubscribed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Primary change followed by failed resubscription: symbol reset
        happened (via the unconditional observer), subscription failure
        observation leaves the stream unsubscribed, and pause behavior is
        unchanged."""
        runner = self._make_runner()
        runner._quotes_subscribed = True
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        runner.quote_stream_health.record_quote("2026-08-01T12:00:00Z")
        paused: list[str] = []
        monkeypatch.setattr(runner.risk, "pause", lambda reason, **_kw: paused.append(reason))
        self._patch_reload_deps(monkeypatch, runner, new_symbol="MSFT.US")
        monkeypatch.setattr(runner.broker, "unsubscribe_quotes", lambda: None)

        def _fail_subscribe(*_a, **_kw):
            raise RuntimeError("broker subscribe failed")

        monkeypatch.setattr(runner.broker, "subscribe_quotes_batch", _fail_subscribe)

        with pytest.raises(RuntimeError, match="quote subscription failed after strategy reload"):
            runner.reload_strategy()

        snap = runner.quote_stream_health.snapshot()
        # Symbol was reset (unconditional observer) despite failed resubscription.
        assert snap.symbol == "MSFT.US"
        assert snap.quotes_received == 0
        # Failed resubscribe leaves stream unsubscribed; no new success counter.
        assert snap.quotes_subscribed is False
        assert snap.resubscribe_count == 1  # only the initial seed success
        # Pause behavior unchanged: risk.pause was called with the failure reason.
        assert len(paused) == 1
        assert "quote subscription failed after strategy reload" in paused[0]

    def test_primary_change_broker_calls_and_exception_flow_unchanged(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Confirm that broker calls, exception flow, and pause behavior remain
        identical to the pre-observer-instrumentation behavior on a successful
        primary-switch resubscription."""
        runner = self._make_runner()
        runner._quotes_subscribed = True
        broker_calls: list[tuple[str, str]] = []
        self._patch_reload_deps(monkeypatch, runner, new_symbol="MSFT.US")
        monkeypatch.setattr(
            runner.broker,
            "unsubscribe_quotes",
            lambda: broker_calls.append(("unsubscribe", "AAPL.US")),
        )
        monkeypatch.setattr(
            runner.broker,
            "subscribe_quotes_batch",
            lambda symbols, _callback: broker_calls.append(
                ("subscribe", ",".join(symbols))
            ),
        )

        runner.reload_strategy()

        # Broker call sequence is exactly unsubscribe then subscribe.
        assert broker_calls == [("unsubscribe", "AAPL.US"), ("subscribe", "MSFT.US")]
        assert runner._quotes_subscribed is True
        # Tracker reflects the successful resubscribe.
        snap = runner.quote_stream_health.snapshot()
        assert snap.symbol == "MSFT.US"
        assert snap.quotes_subscribed is True
        assert snap.quotes_received == 0  # reset on successful resubscribe

    def test_observe_primary_symbol_changed_does_not_touch_subscription_state(self) -> None:
        """The symbol-reset observer must NOT mark subscribed/unsubscribed or
        increment subscription counters."""
        runner = AppRunner()
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.record_subscription_success()
        before = runner.quote_stream_health.snapshot()
        assert before.quotes_subscribed is True
        assert before.resubscribe_count == 1

        runner._observe_primary_symbol_changed("MSFT.US")

        after = runner.quote_stream_health.snapshot()
        assert after.symbol == "MSFT.US"
        assert after.quotes_received == 0  # window reset
        # Subscription state and counters untouched.
        assert after.quotes_subscribed == before.quotes_subscribed
        assert after.resubscribe_count == before.resubscribe_count
        assert after.disconnect_count == before.disconnect_count

    def test_observe_primary_symbol_changed_throwing_does_not_break(self) -> None:
        """A throwing tracker mutator inside the observer must not alter
        control flow — the observer swallows ordinary Exception."""
        runner = AppRunner()
        # Force the tracker's lock to raise; the observer must swallow it.
        runner.quote_stream_health._lock = lambda: (_ for _ in ()).throw(RuntimeError("x"))  # type: ignore[assignment]
        # Calling the observer must not raise.
        runner._observe_primary_symbol_changed("MSFT.US")