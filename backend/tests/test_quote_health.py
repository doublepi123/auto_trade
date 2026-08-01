"""Quote stream health tracker and API — process-local, fake-clock, read-only.

Covers: initial/healthy/stale states, gap counting/max gap, disconnect/
resubscribe counters, safe API projection, and focused runner regression
coverage ensuring the tracker hooks do not alter control flow.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from app.api import quote_health as quote_health_api
from app.config import settings
from app.core.broker import Quote
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
    def test_initial_state_is_disconnected_with_no_quotes(self, tracker) -> None:
        snap = tracker.snapshot()
        assert snap.status == "disconnected"
        assert snap.quotes_received == 0
        assert snap.last_quote_timestamp is None
        assert snap.last_quote_age_seconds is None
        assert snap.max_gap_seconds == 0.0
        assert snap.disconnect_count == 0
        assert snap.resubscribe_count == 0
        assert snap.disconnect_retry_count == 0
        assert snap.quotes_subscribed is False
        assert snap.symbol == "AAPL.US"

    def test_subscribed_but_no_quotes_is_initial(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        snap = tracker.snapshot()
        assert snap.status == "initial"
        assert snap.quotes_subscribed is True


class TestQuoteStreamHealthHealthy:
    def test_record_quote_increments_count_and_timestamp(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        snap = tracker.snapshot()
        assert snap.quotes_received == 1
        assert snap.last_quote_timestamp == "2026-08-01T12:00:00Z"
        assert snap.last_quote_age_seconds == 0.0
        assert snap.status == "healthy"

    def test_age_advances_with_clock(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        # Advance the monotonic clock 30s.
        tracker._now_monotonic.advance(30.0)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.last_quote_age_seconds == 30.0
        assert snap.status == "healthy"

    def test_stale_after_90s_without_quote(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker._now_monotonic.advance(90.01)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.status == "stale"
        assert snap.last_quote_age_seconds == pytest.approx(90.01)

    def test_just_under_90s_is_still_healthy(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker._now_monotonic.advance(89.99)  # type: ignore[attr-defined]
        snap = tracker.snapshot()
        assert snap.status == "healthy"


class TestQuoteStreamHealthGap:
    def test_max_gap_tracks_largest_delta(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("2026-08-01T12:00:05Z")  # +5s
        tracker.record_quote("2026-08-01T12:00:12Z")  # +7s (new max)
        tracker.record_quote("2026-08-01T12:00:14Z")  # +2s (not max)
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 7.0

    def test_out_of_order_quote_does_not_advance_max_gap(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:10Z")
        tracker.record_quote("2026-08-01T12:00:05Z")  # older — ignored for gap
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 0.0

    def test_resubscribe_resets_gap_window(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("2026-08-01T12:00:30Z")  # 30s gap
        assert tracker.snapshot().max_gap_seconds == 30.0
        tracker.record_resubscribe()
        # After resubscribe, the next quote starts a fresh gap window.
        tracker.record_quote("2026-08-01T13:00:00Z")
        tracker.record_quote("2026-08-01T13:00:10Z")
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 10.0  # only the post-resubscribe gap

    def test_unparseable_timestamp_does_not_break_or_advance_gap(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_quote("not-a-timestamp")  # unparseable
        tracker.record_quote("2026-08-01T12:00:20Z")
        snap = tracker.snapshot()
        # The unparseable quote was still counted in quotes_received.
        assert snap.quotes_received == 3
        # Gap is from the last good timestamp to the next good one (20s).
        assert snap.max_gap_seconds == 20.0

    def test_epoch_millis_timestamp_parsed(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("1754049600000")  # 2026-08-01T12:00:00Z in ms
        tracker.record_quote("1754049605000")  # +5s
        snap = tracker.snapshot()
        assert snap.max_gap_seconds == 5.0


class TestQuoteStreamHealthDisconnect:
    def test_record_disconnect_increments_and_marks_unsubscribed(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
        tracker.record_quote("2026-08-01T12:00:00Z")
        tracker.record_disconnect()
        snap = tracker.snapshot()
        assert snap.disconnect_count == 1
        assert snap.quotes_subscribed is False
        assert snap.status == "disconnected"

    def test_record_disconnect_retry_increments_separately(self, tracker) -> None:
        tracker.record_disconnect_retry()
        tracker.record_disconnect_retry()
        snap = tracker.snapshot()
        assert snap.disconnect_retry_count == 2
        # disconnect_count is separate.
        assert snap.disconnect_count == 0

    def test_record_resubscribe_increments_and_marks_subscribed(self, tracker) -> None:
        tracker.record_disconnect()
        tracker.record_resubscribe()
        snap = tracker.snapshot()
        assert snap.resubscribe_count == 1
        assert snap.quotes_subscribed is True


class TestQuoteStreamHealthReset:
    def test_reset_clears_all_counters(self, tracker) -> None:
        tracker.set_quotes_subscribed(True)
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
        assert snap.status == "disconnected"


class TestQuoteStreamHealthMutatorSafety:
    def test_record_quote_swallows_internal_errors(self, tracker) -> None:
        def boom() -> float:
            raise RuntimeError("clock broken")

        tracker._now_monotonic = boom  # type: ignore[assignment]
        tracker.record_quote("2026-08-01T12:00:00Z")  # must not raise

    def test_record_disconnect_swallows_internal_errors(self, tracker) -> None:
        def boom() -> None:
            raise RuntimeError("lock broken")

        original = tracker._lock
        tracker._lock = boom  # type: ignore[assignment]
        tracker.record_disconnect()  # must not raise
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

    def test_endpoint_returns_safe_snapshot(self, monkeypatch: pytest.MonkeyPatch) -> None:
        mono = _FakeMonotonicClock()
        dt = _FakeDateTimeClock(datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc))
        fake_tracker = QuoteStreamHealthTracker(
            "AAPL.US",
            now_monotonic=mono,
            now_datetime=dt,
        )
        fake_tracker.set_quotes_subscribed(True)
        fake_tracker.record_quote("2026-08-01T12:00:00Z")
        monkeypatch.setattr(
            quote_health_api,
            "get_quote_stream_health_tracker",
            lambda: fake_tracker,
        )
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
        fake_tracker.set_quotes_subscribed(True)
        fake_tracker.record_quote("2026-08-01T12:00:00Z")
        monkeypatch.setattr(
            quote_health_api,
            "get_quote_stream_health_tracker",
            lambda: fake_tracker,
        )
        body = self.client.get("/api/quote-health").text
        # No price/order/credential material exposed.
        assert "last_price" not in body
        assert "bid" not in body
        assert "ask" not in body
        assert "order_id" not in body

    def test_endpoint_returns_disconnected_when_runner_unavailable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # When the runner has no bound tracker, the endpoint returns a fresh
        # empty tracker (all-zero, disconnected).
        class _NoTracker:
            pass

        import app.runner as runner_module

        monkeypatch.setattr(runner_module, "get_runner", lambda: _NoTracker())
        resp = self.client.get("/api/quote-health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "disconnected"
        assert data["quotes_received"] == 0
        assert data["quotes_subscribed"] is False

    def test_auth_enforced_when_api_key_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(settings, "api_key", "qh-secret")
        assert self.client.get("/api/quote-health").status_code == 401
        resp = self.client.get("/api/quote-health", headers={"X-API-Key": "qh-secret"})
        assert resp.status_code == 200


class TestRunnerQuoteHealthIntegration:
    """Focused runner regression coverage: the tracker hooks do not alter
    control flow and record the expected events."""

    def test_runner_has_quote_stream_health_attribute(self) -> None:
        runner = AppRunner()
        assert isinstance(runner.quote_stream_health, QuoteStreamHealthTracker)

    def test_record_quote_hooked_in_remember_quote(self) -> None:
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.set_quotes_subscribed(True)
        # Feed a trusted primary quote through _remember_quote. The timestamp
        # must be fresh (within _QUOTE_SOURCE_MAX_AGE_SECONDS of now) so the
        # quality gate marks it trusted.
        fresh_ts = datetime.now(timezone.utc).isoformat()
        quote = Quote(
            symbol="AAPL.US",
            last_price=100.0,
            bid=99.5,
            ask=100.5,
            timestamp=fresh_ts,
        )
        runner._remember_quote(quote)
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_received == 1
        assert snap.last_quote_timestamp == fresh_ts

    def test_record_quote_ignores_non_primary_symbol(self) -> None:
        runner = AppRunner()
        runner.engine.params.symbol = "AAPL.US"
        runner.engine.params.market = "US"
        runner.quote_stream_health.set_symbol("AAPL.US")
        runner.quote_stream_health.set_quotes_subscribed(True)
        # A secondary symbol quote must not be counted as primary health.
        quote = Quote(
            symbol="MSFT.US",
            last_price=300.0,
            bid=299.5,
            ask=300.5,
            timestamp="2026-08-01T12:00:00Z",
        )
        runner._remember_quote(quote)
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
        runner.quote_stream_health.set_quotes_subscribed(True)
        runner._running = True
        # stop() joins the thread; bypass by setting _thread to None.
        runner._thread = None
        runner.stop()
        snap = runner.quote_stream_health.snapshot()
        assert snap.quotes_subscribed is False