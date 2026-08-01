"""Process-local quote stream health metrics for the runner's primary symbol.

A small, dependency-free observer that records the minimum counters needed to
report quote-stream health on ``GET /api/quote-health``: quotes received, last
quote timestamp/age, maximum observed gap between consecutive trusted primary
push quotes, disconnect count, resubscribe count, and disconnect retry count.

Design constraints (P0 live safety):

* The tracker never subscribes, reconnects, pauses, calls the broker, or
  performs any I/O/database work. It is pure in-memory state.
* Callback instrumentation is constant-time and non-blocking: each mutator
  does at most one timestamp parse, one float comparison, and a few integer
  increments under a single short lock. It never acquires the runner's
  ``_state_lock``.
* The tracker does not change the existing quote freshness threshold or any
  pause/recovery decision. It only observes.
* All mutators are best-effort and swallow ordinary ``Exception`` only —
  never ``BaseException``/``CancelledError`` — so observer failures cannot
  alter broker calls, pause/recovery decisions, or exception flow.

Counter semantics (documented explicitly):

* ``quotes_received`` — count of trusted primary **push-stream** quotes
  received during the current subscription window. Active polling/trusted
  refresh (``is_push=False``) does NOT increment this counter or update
  freshness, so a polling refresh cannot mask a silent push stream. Reset to
  ``0`` on a successful new subscription/reconnect and on a primary-symbol
  change.
* ``max_gap_seconds`` — maximum delta between consecutive trusted primary
  push quote *source* timestamps during the *current subscription window*.
  Reset to ``0.0`` on a successful new subscription/reconnect and on a
  primary-symbol change. Only monotonically-increasing source timestamps
  advance the max: out-of-order timestamps are ignored entirely (neither
  advance the max gap nor update the last-source-time reference).
* ``disconnect_count`` — **process-lifetime** count of broker disconnect
  events. Never reset.
* ``resubscribe_count`` — **process-lifetime** count of successful
  subscription/resubscription events (initial start, silent-watchdog
  resubscribe, reconnect, credential reload, primary-symbol change). Never
  reset.
* ``disconnect_retry_count`` — **process-lifetime** count of disconnect
  retry attempts. Never reset.

Subscription-window state (``quotes_received``, ``last_quote_timestamp``,
``last_quote_at``, ``last_source_time``, ``max_gap_seconds``) is reset on
every successful new subscription/reconnect so old quote age cannot report
healthy before the first new push quote. A failed subscription/resubscription
leaves the stream unsubscribed/unavailable (``quotes_subscribed=False``),
never subscribed.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
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

    Constructed by the runner and mutated from the existing quote/disconnect/
    subscription code paths via narrow no-throw methods. All mutators swallow
    ordinary ``Exception`` only so observer failures never alter control flow.
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
        # Process-lifetime counters (never reset).
        self._disconnect_count: int = 0
        self._resubscribe_count: int = 0
        self._disconnect_retry_count: int = 0
        self._quotes_subscribed: bool = False

    # --- symbol / subscription lifecycle (no-throw) ----------------------

    def set_symbol(self, symbol: str) -> None:
        """Update the primary symbol and reset symbol-bound quote state.

        A primary-symbol change must not carry over the previous symbol's
        quote count/arrival/gap state, so the current subscription window's
        symbol-bound state is reset. Process-lifetime counters
        (disconnect/resubscribe/retry) are preserved.
        """
        try:
            with self._lock:
                self._symbol = symbol
                self._reset_window_locked()
        except Exception:
            logger.debug("quote-health set_symbol failed", exc_info=True)

    def record_subscription_success(self) -> None:
        """Record a successful new subscription/reconnect.

        Resets the current subscription window's quote-arrival/gap state so
        old quote age cannot report healthy before the first new push quote.
        Increments the process-lifetime resubscribe counter.
        """
        try:
            with self._lock:
                self._resubscribe_count += 1
                self._quotes_subscribed = True
                self._reset_window_locked()
        except Exception:
            logger.debug("quote-health record_subscription_success failed", exc_info=True)

    def record_subscription_failure(self) -> None:
        """Record a failed subscription/resubscription.

        The stream remains unsubscribed/unavailable (``quotes_subscribed``
        stays ``False``). Does not increment resubscribe_count (no successful
        subscription occurred).
        """
        try:
            with self._lock:
                self._quotes_subscribed = False
        except Exception:
            logger.debug("quote-health record_subscription_failure failed", exc_info=True)

    def set_quotes_subscribed(self, value: bool) -> None:
        """Directly set the subscribed flag (low-level; prefer the
        ``record_subscription_*`` methods for lifecycle events)."""
        try:
            with self._lock:
                self._quotes_subscribed = bool(value)
        except Exception:
            logger.debug("quote-health set_quotes_subscribed failed", exc_info=True)

    def _reset_window_locked(self) -> None:
        """Reset current-subscription-window state (caller holds the lock)."""
        self._quotes_received = 0
        self._last_quote_timestamp = None
        self._last_quote_at = _NO_QUOTE
        self._last_source_time = None
        self._max_gap_seconds = 0.0

    # --- quote recording (push-stream only, no-throw) --------------------

    def record_quote(self, timestamp: str) -> None:
        """Record a trusted primary **push-stream** quote arrival.

        Constant-time: one timestamp parse, one float comparison, increments.
        Never raises into the caller. Out-of-order source timestamps are
        ignored entirely (neither advance the max gap nor update the
        last-source-time reference).
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
                    if prev is None or source_time > prev:
                        if prev is not None:
                            gap = (source_time - prev).total_seconds()
                            if gap > self._max_gap_seconds:
                                self._max_gap_seconds = gap
                        self._last_source_time = source_time
        except Exception:
            logger.debug("quote-health record_quote failed", exc_info=True)

    # --- disconnect / retry (process-lifetime, no-throw) -----------------

    def record_disconnect(self) -> None:
        try:
            with self._lock:
                self._disconnect_count += 1
                self._quotes_subscribed = False
        except Exception:
            logger.debug("quote-health record_disconnect failed", exc_info=True)

    def record_resubscribe(self) -> None:
        """Record a successful resubscribe (alias for record_subscription_success)."""
        self.record_subscription_success()

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
        """Clear all counters including process-lifetime (tests only)."""
        with self._lock:
            self._reset_window_locked()
            self._disconnect_count = 0
            self._resubscribe_count = 0
            self._disconnect_retry_count = 0
            self._quotes_subscribed = False

    # --- read projection -------------------------------------------------

    def snapshot(self) -> QuoteStreamHealthSnapshot:
        """Return a safe, immutable projection of the current state.

        All mutable state is copied into the immutable snapshot dataclass
        while holding the lock; classification happens inside the lock.
        """
        try:
            now_mono = self._now_monotonic()
            with self._lock:
                last_quote_at = self._last_quote_at
                last_quote_age: float | None
                if last_quote_at <= _NO_QUOTE:
                    last_quote_age = None
                else:
                    last_quote_age = max(0.0, now_mono - last_quote_at)
                status = self._classify_locked(now_mono)
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
        except Exception:
            logger.debug("quote-health snapshot failed", exc_info=True)
            # Return a safe unavailable projection rather than raising.
            return QuoteStreamHealthSnapshot(
                symbol=self._symbol,
                quotes_received=0,
                last_quote_timestamp=None,
                last_quote_age_seconds=None,
                max_gap_seconds=0.0,
                disconnect_count=0,
                resubscribe_count=0,
                disconnect_retry_count=0,
                quotes_subscribed=False,
                status="unavailable",
                as_of=self._now_datetime(),
            )

    def _classify_locked(self, now_mono: float) -> str:
        """Classify the stream status (caller holds the lock).

        ``unavailable`` — not currently subscribed.
        ``waiting`` — subscribed but no push quote received yet in this window.
        ``healthy`` — subscribed and a recent push quote.
        ``stale``  — subscribed but no recent push quote (age > 90s, matching
        the runner's existing quote-silence watchdog threshold without
        coupling to its pause decision).
        """
        if not self._quotes_subscribed:
            return "unavailable"
        if self._last_quote_at <= _NO_QUOTE:
            return "waiting"
        age = max(0.0, now_mono - self._last_quote_at)
        # 90s mirrors the runner's _quote_resubscribe_threshold_seconds default.
        if age > 90.0:
            return "stale"
        return "healthy"