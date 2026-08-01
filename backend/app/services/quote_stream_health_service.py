"""Process-local quote stream health metrics for the runner's primary symbol.

A small, dependency-free observer that records the minimum counters needed to
report quote-stream health on ``GET /api/quote-health``: quotes received, last
quote timestamp/age, maximum observed gap between consecutive trusted primary
quotes, disconnect count, resubscribe count, and disconnect retry count.

Design constraints (P0 live safety):

* The tracker never subscribes, reconnects, pauses, calls the broker, or
  performs any I/O/database work. It is pure in-memory state.
* Callback instrumentation is constant-time and non-blocking: each mutator
  does at most one timestamp parse, one float comparison, and a few integer
  increments under a single short lock. It never acquires the runner's
  ``_state_lock``.
* The tracker does not change the existing quote freshness threshold or any
  pause/recovery decision. It only observes.
* ``max_gap_seconds`` is the maximum delta between consecutive trusted primary
  quote *source* timestamps observed during the *current subscription window*.
  It is reset to ``0.0`` on :meth:`reset_gap` (called when the stream is
  resubscribed or reconnects), so a gap across a disconnect is not counted as
  an in-stream gap and the metric reflects the current stream's quality, not a
  process-lifetime accumulator. A gap is only counted between two
  successfully-parsed, monotonically-increasing source timestamps; out-of-order
  or unparseable timestamps do not advance the max.
* ``last_quote_age_seconds`` is computed against an injected ``now_monotonic``
  callable so tests are deterministic. In production it uses
  ``time.monotonic``.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Final

logger = logging.getLogger("auto_trade.quote_health")

#: Sentinel for "no quote received yet".
_NO_QUOTE: Final[float] = 0.0


def _parse_source_timestamp(raw: str) -> datetime | None:
    """Parse a quote source timestamp string to aware UTC.

    Accepts both epoch seconds/millis (numeric strings) and ISO 8601. Returns
    ``None`` if the value is empty or unparseable. Mirrors the runner's
    ``_quote_source_timestamp_is_fresh`` parsing but never raises.
    """
    value = (raw or "").strip()
    if not value:
        return None
    try:
        if value.replace(".", "", 1).isdigit():
            numeric = float(value)
            if numeric > 10_000_000_000:
                numeric /= 1000
            return datetime.fromtimestamp(numeric, tz=timezone.utc)
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError, OSError):
        return None


@dataclass(frozen=True)
class QuoteStreamHealthSnapshot:
    """Immutable safe projection of quote-stream health for the API."""

    symbol: str
    quotes_received: int
    last_quote_timestamp: str | None
    last_quote_age_seconds: float | None
    max_gap_seconds: float
    disconnect_count: int
    resubscribe_count: int
    disconnect_retry_count: int
    quotes_subscribed: bool
    status: str
    as_of: datetime


class QuoteStreamHealthTracker:
    """Process-local quote stream health metrics (observer-only).

    Constructed by the runner and mutated from the existing quote/disconnect
    code paths. All mutators are best-effort and swallow their own errors so a
    tracker bug cannot break a quote callback.
    """

    def __init__(
        self,
        symbol: str = "",
        *,
        now_monotonic: Callable[[], float] = time.monotonic,
        now_datetime: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self._symbol = symbol
        self._now_monotonic = now_monotonic
        self._now_datetime = now_datetime
        self._lock = threading.Lock()
        self._quotes_received: int = 0
        self._last_quote_timestamp: str | None = None
        self._last_quote_at: float = _NO_QUOTE
        self._last_source_time: datetime | None = None
        self._max_gap_seconds: float = 0.0
        self._disconnect_count: int = 0
        self._resubscribe_count: int = 0
        self._disconnect_retry_count: int = 0
        self._quotes_subscribed: bool = False

    def set_symbol(self, symbol: str) -> None:
        with self._lock:
            self._symbol = symbol

    def set_quotes_subscribed(self, value: bool) -> None:
        with self._lock:
            self._quotes_subscribed = bool(value)

    def record_quote(self, timestamp: str) -> None:
        """Record a trusted primary quote arrival.

        Constant-time: one timestamp parse, one float comparison, increments.
        Never raises into the caller.
        """
        try:
            source_time = _parse_source_timestamp(timestamp)
            now_mono = self._now_monotonic()
            with self._lock:
                self._quotes_received += 1
                self._last_quote_timestamp = timestamp or None
                self._last_quote_at = now_mono
                if source_time is not None:
                    prev = self._last_source_time
                    if prev is not None and source_time > prev:
                        gap = (source_time - prev).total_seconds()
                        if gap > self._max_gap_seconds:
                            self._max_gap_seconds = gap
                    self._last_source_time = source_time
        except Exception:
            logger.debug("quote-health record_quote failed", exc_info=True)

    def record_disconnect(self) -> None:
        try:
            with self._lock:
                self._disconnect_count += 1
                self._quotes_subscribed = False
        except Exception:
            logger.debug("quote-health record_disconnect failed", exc_info=True)

    def record_resubscribe(self) -> None:
        try:
            with self._lock:
                self._resubscribe_count += 1
                self._quotes_subscribed = True
                # A resubscribe starts a fresh in-stream gap window and resets
                # the max-gap accumulator so the metric reflects the current
                # stream's quality, not a cross-disconnect or process-lifetime
                # value.
                self._last_source_time = None
                self._max_gap_seconds = 0.0
        except Exception:
            logger.debug("quote-health record_resubscribe failed", exc_info=True)

    def record_disconnect_retry(self) -> None:
        try:
            with self._lock:
                self._disconnect_retry_count += 1
        except Exception:
            logger.debug("quote-health record_disconnect_retry failed", exc_info=True)

    def reset_gap(self) -> None:
        """Reset the max-gap window and accumulator (e.g. after a reconnect)."""
        try:
            with self._lock:
                self._last_source_time = None
                self._max_gap_seconds = 0.0
        except Exception:
            logger.debug("quote-health reset_gap failed", exc_info=True)

    def reset(self) -> None:
        """Clear all counters (tests only)."""
        with self._lock:
            self._quotes_received = 0
            self._last_quote_timestamp = None
            self._last_quote_at = _NO_QUOTE
            self._last_source_time = None
            self._max_gap_seconds = 0.0
            self._disconnect_count = 0
            self._resubscribe_count = 0
            self._disconnect_retry_count = 0
            self._quotes_subscribed = False

    def snapshot(self) -> QuoteStreamHealthSnapshot:
        """Return a safe, immutable projection of the current state."""
        now_mono = self._now_monotonic()
        with self._lock:
            last_quote_at = self._last_quote_at
            last_quote_age: float | None
            if last_quote_at <= _NO_QUOTE:
                last_quote_age = None
            else:
                last_quote_age = max(0.0, now_mono - last_quote_at)
            status = self._classify_locked()
            return QuoteStreamHealthSnapshot(
                symbol=self._symbol,
                quotes_received=self._quotes_received,
                last_quote_timestamp=self._last_quote_timestamp,
                last_quote_age_seconds=last_quote_age,
                max_gap_seconds=self._max_gap_seconds,
                disconnect_count=self._disconnect_count,
                resubscribe_count=self._resubscribe_count,
                disconnect_retry_count=self._disconnect_retry_count,
                quotes_subscribed=self._quotes_subscribed,
                status=status,
                as_of=self._now_datetime(),
            )

    def _classify_locked(self) -> str:
        """Classify the stream status (caller holds the lock).

        ``initial`` — never received a quote.
        ``healthy`` — subscribed and a recent quote.
        ``stale``  — subscribed but no recent quote (age > 90s, matching the
        runner's existing quote-silence watchdog threshold without coupling
        to its pause decision).
        ``disconnected`` — not currently subscribed.
        """
        if not self._quotes_subscribed:
            return "disconnected"
        if self._last_quote_at <= _NO_QUOTE:
            return "initial"
        age = max(0.0, self._now_monotonic() - self._last_quote_at)
        # 90s mirrors the runner's _quote_resubscribe_threshold_seconds default.
        if age > 90.0:
            return "stale"
        return "healthy"