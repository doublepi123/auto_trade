"""In-memory retry queue for failed notification deliveries.

When ``MultiChannelNotifier.send`` finds every configured channel failed,
it currently only logs a warning. Critical alerts (KILL_SWITCH, broker
outage) deserve a retry: the channel might be temporarily unreachable
(5xx from a webhook receiver, transient DNS failure) and a few-second
exponential backoff with a bounded attempt count is enough to recover.

Design constraints:
  * The queue lives in-process (per replica). A multi-replica deploy would
    need a shared backend; for the current single-instance Docker setup
    an in-memory deque is sufficient and avoids a new persistence layer.
  * Each entry holds a (title, content, severity) tuple plus a monotonically
    increasing attempt counter. The retry worker reads the front of the
    queue, calls ``notifier.send`` again, and either pops the entry (on
    success or after exceeding the max attempts) or re-queues it with a
    longer backoff.
  * The retry worker is started lazily on the first failed send and runs
    in a daemon thread. It can be stopped on app shutdown.

This module is intentionally decoupled from any specific notifier so the
runner can install a single global notifier and have it transparently
covered.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Deque, Optional

logger = logging.getLogger("auto_trade.notify.retry")


@dataclass
class _Pending:
    title: str
    content: str
    severity: str
    attempts: int = 0
    next_attempt_at: float = 0.0
    last_error: str = ""


class NotificationRetryQueue:
    """Bounded retry queue with exponential backoff.

    A ``send`` callable is injected at construction time so the queue can
    be tested without a real notifier and so callers retain control over
    the dispatcher (e.g. they may want to filter by severity before
    re-invoking the notifier).
    """

    DEFAULT_MAX_ATTEMPTS = 4
    DEFAULT_INITIAL_BACKOFF = 2.0  # seconds
    DEFAULT_MAX_BACKOFF = 60.0
    DEFAULT_QUEUE_CAPACITY = 256

    def __init__(
        self,
        send_fn: Callable[[str, str, str], bool],
        *,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        initial_backoff: float = DEFAULT_INITIAL_BACKOFF,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        capacity: int = DEFAULT_QUEUE_CAPACITY,
    ) -> None:
        self._send = send_fn
        self._max_attempts = max(1, max_attempts)
        self._initial_backoff = max(0.1, initial_backoff)
        self._max_backoff = max(self._initial_backoff, max_backoff)
        self._capacity = max(1, capacity)
        self._queue: Deque[_Pending] = deque()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._metrics = {
            "enqueued": 0,
            "dropped_capacity": 0,
            "delivered": 0,
            "exhausted": 0,
        }

    # -------- public API --------

    def enqueue(self, title: str, content: str, severity: str, error: str = "") -> bool:
        """Schedule a retry. Returns False if the queue is full."""
        with self._lock:
            if len(self._queue) >= self._capacity:
                self._metrics["dropped_capacity"] += 1
                logger.warning(
                    "notification retry queue full (capacity=%d); dropping title=%s",
                    self._capacity,
                    title,
                )
                return False
            self._queue.append(
                _Pending(
                    title=title,
                    content=content,
                    severity=severity,
                    attempts=0,
                    next_attempt_at=time.monotonic() + self._initial_backoff,
                    last_error=error,
                )
            )
            self._metrics["enqueued"] += 1
        self._ensure_worker()
        self._wake_event.set()
        return True

    def start(self) -> None:
        """Start the background worker. Idempotent."""
        self._ensure_worker()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the worker to stop and wait briefly for it to drain."""
        self._stop_event.set()
        self._wake_event.set()
        thread = self._worker_thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def drain(self) -> int:
        """Process every queued item synchronously. Returns delivered count.

        Useful for tests and for the shutdown path where we want to give
        pending notifications one last chance before tearing the process down.
        """
        delivered = 0
        while True:
            with self._lock:
                if not self._queue:
                    break
                pending = self._queue.popleft()
            if self._attempt(pending):
                delivered += 1
        return delivered

    def metrics(self) -> dict[str, int]:
        with self._lock:
            return dict(self._metrics)

    def pending_count(self) -> int:
        with self._lock:
            return len(self._queue)

    # -------- worker --------

    def _ensure_worker(self) -> None:
        if self._worker_thread is not None and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._run, name="notification-retry", daemon=True
        )
        self._worker_thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            now = time.monotonic()
            pending = self._pop_due(now)
            if pending is None:
                # Sleep until the next item is due, or wake up if one is
                # enqueued. Use a short cap so stop() does not have to
                # wait the full backoff window.
                next_due = self._next_due()
                if next_due is None:
                    self._wake_event.wait(timeout=1.0)
                else:
                    wait = max(0.0, min(next_due - now, 1.0))
                    self._wake_event.wait(timeout=wait)
                self._wake_event.clear()
                continue
            self._attempt(pending)

    def _pop_due(self, now: float) -> Optional[_Pending]:
        with self._lock:
            if not self._queue:
                return None
            head = self._queue[0]
            if head.next_attempt_at > now:
                return None
            return self._queue.popleft()

    def _next_due(self) -> Optional[float]:
        with self._lock:
            if not self._queue:
                return None
            return self._queue[0].next_attempt_at

    def _attempt(self, pending: _Pending) -> bool:
        pending.attempts += 1
        try:
            ok = self._send(pending.title, pending.content, pending.severity)
        except Exception as exc:
            ok = False
            pending.last_error = repr(exc)
        if ok:
            with self._lock:
                self._metrics["delivered"] += 1
            return True
        if pending.attempts >= self._max_attempts:
            with self._lock:
                self._metrics["exhausted"] += 1
            logger.warning(
                "notification retry exhausted after %d attempts: title=%s severity=%s last_error=%s",
                pending.attempts,
                pending.title,
                pending.severity,
                pending.last_error,
            )
            return False
        # Exponential backoff with a cap. Attempt 1 already happened
        # before enqueue, so attempt 2 waits initial_backoff, attempt 3
        # waits 2x, etc.
        backoff = min(
            self._initial_backoff * (2 ** (pending.attempts - 1)),
            self._max_backoff,
        )
        pending.next_attempt_at = time.monotonic() + backoff
        with self._lock:
            self._queue.append(pending)
        return False
