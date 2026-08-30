from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

_HEALTHCHECK_PATHS = ("/api/ready", "/api/live")


class RepeatedLogThrottle:
    """Keyed rate limiter for periodically repeated log lines.

    Mirrors the notification dedupe idiom (``MultiChannelNotifier``): a
    ``threading.Lock`` plus a ``time.monotonic()`` window. Suppressed
    occurrences are counted, never discarded — the next emission reports
    them via :meth:`take_suppressed_count`.
    """

    def __init__(
        self,
        *,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._window_seconds = window_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._last_emitted_at: dict[str, float] = {}
        self._suppressed = 0

    def should_log(self, key: str) -> bool:
        now = self._clock()
        with self._lock:
            last = self._last_emitted_at.get(key)
            if last is not None and (now - last) < self._window_seconds:
                self._suppressed += 1
                return False
            self._last_emitted_at[key] = now
            return True

    @property
    def suppressed_count(self) -> int:
        with self._lock:
            return self._suppressed

    def take_suppressed_count(self) -> int:
        with self._lock:
            count = self._suppressed
            self._suppressed = 0
            return count


class HealthcheckAccessFilter(logging.Filter):
    """Drop uvicorn access-log lines for container healthcheck probes.

    The Docker healthcheck polls ``/api/ready`` every ~30s; those 200s are
    noise, not operator signal. Only exact-path GETs are dropped — a real
    request to ``/api/ready/export`` or a 500 on the probe still passes.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if record.name != "uvicorn.access" or not isinstance(args, tuple) or len(args) < 4:
            return True
        method = args[1]
        path = args[2]
        if method != "GET":
            return True
        return path not in _HEALTHCHECK_PATHS
