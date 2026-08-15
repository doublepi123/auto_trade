"""Event-loop liveness watchdog.

The 2026-08-08 production freeze showed that a single indefinitely blocked
synchronous call on the asyncio event loop (e.g. waiting on a
``threading.Lock`` held by a thread hung inside a broker SDK FFI call) stops
HTTP serving, cron jobs, and all database writes while the process stays
alive. Docker's healthcheck can only *mark* such a container unhealthy; it
never restarts it.

This module provides a last-resort in-process watchdog:

* an asyncio heartbeat task calls :meth:`LivenessWatchdog.beat` every few
  seconds, proving the event loop is still scheduling callbacks;
* an independent daemon thread (no asyncio, no broker FFI, no DB) measures
  the heartbeat age and, once it exceeds the stale threshold:

  1. dumps every thread's traceback via :mod:`faulthandler` to stderr so
     the *next* freeze leaves forensic evidence in the container logs;
  2. after the hard-exit threshold, terminates the process with
     ``os._exit`` so the Docker restart policy can revive it.

The watchdog thread deliberately avoids asyncio, broker FFI, database work,
and normal logging on its hard-exit path so the application deadlock cannot
prevent recovery.
"""

from __future__ import annotations

import faulthandler
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

logger = logging.getLogger(__name__)


class LivenessWatchdog:
    """Detects a frozen event loop, dumps tracebacks, and self-terminates."""

    def __init__(
        self,
        *,
        stale_after_seconds: float,
        hard_exit_after_seconds: float,
        beat_interval_seconds: float,
        dump_traceback_enabled: bool = True,
        hard_exit_enabled: bool = True,
        monotonic: Callable[[], float] = time.monotonic,
        exit_fn: Callable[[int], None] = os._exit,
        dump_stream: Any = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("stale_after_seconds must be positive")
        if hard_exit_after_seconds <= stale_after_seconds:
            raise ValueError("hard_exit_after_seconds must exceed stale_after_seconds")
        if beat_interval_seconds <= 0:
            raise ValueError("beat_interval_seconds must be positive")
        if beat_interval_seconds >= stale_after_seconds:
            raise ValueError("beat_interval_seconds must be shorter than stale_after_seconds")
        self._stale_after = stale_after_seconds
        self._hard_exit_after = hard_exit_after_seconds
        self._beat_interval = beat_interval_seconds
        self._dump_enabled = dump_traceback_enabled
        self._hard_exit_enabled = hard_exit_enabled
        self._monotonic = monotonic
        self._exit_fn = exit_fn
        self._dump_stream = dump_stream if dump_stream is not None else sys.stderr
        self._lock = threading.Lock()
        self._last_beat = self._monotonic()
        self._last_dump_at: float | None = None
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._started = False

    @property
    def stale_after_seconds(self) -> float:
        return self._stale_after

    def beat(self) -> None:
        """Record a heartbeat; called by the asyncio heartbeat task."""
        with self._lock:
            self._last_beat = self._monotonic()
            # A recovered loop starts a new incident. Its next freeze should
            # produce a fresh dump immediately instead of inheriting cooldown.
            self._last_dump_at = None

    def beat_age_seconds(self) -> float:
        with self._lock:
            return self._monotonic() - self._last_beat

    def is_alive(self) -> bool:
        return self.beat_age_seconds() <= self._stale_after

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._last_beat = self._monotonic()
            self._last_dump_at = None
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._watch_loop,
            name="liveness-watchdog",
            daemon=True,
        )
        try:
            self._thread.start()
        except Exception:
            with self._lock:
                self._started = False
                self._thread = None
            raise
        logger.info(
            "liveness watchdog started (stale_after=%gs, hard_exit_after=%gs, "
            "dump=%s, hard_exit=%s)",
            self._stale_after,
            self._hard_exit_after,
            self._dump_enabled,
            self._hard_exit_enabled,
        )

    def stop(self) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        with self._lock:
            self._started = False
            self._thread = None

    def _dump_tracebacks(self, age: float) -> None:
        logger.critical(
            "event loop heartbeat stale for %.1fs (threshold %.1fs); dumping all thread tracebacks",
            age,
            self._stale_after,
        )
        try:
            faulthandler.dump_traceback(file=self._dump_stream, all_threads=True)
        except Exception:
            logger.exception("faulthandler dump failed")

    def _schedule_traceback_dump(self, age: float) -> None:
        # stderr and logging locks can themselves be wedged. Keep all
        # forensic output off the watchdog thread so the hard-exit deadline
        # remains reachable even when dumping blocks indefinitely.
        threading.Thread(
            target=self._dump_tracebacks,
            args=(age,),
            name="liveness-traceback-dump",
            daemon=True,
        ).start()

    def _watch_loop(self) -> None:
        check_interval = max(0.05, min(self._beat_interval, 10.0))
        dump_cooldown = 60.0
        while not self._stop_event.wait(check_interval):
            age = self.beat_age_seconds()
            if age <= self._stale_after:
                continue
            # This path must not acquire logging or stderr locks before exit.
            # os._exit is the final recovery mechanism when those locks or the
            # dump thread are part of the freeze.
            if self._hard_exit_enabled and age > self._hard_exit_after:
                self._exit_fn(1)
                return
            now = self._monotonic()
            if self._dump_enabled and (
                self._last_dump_at is None or now - self._last_dump_at >= dump_cooldown
            ):
                self._last_dump_at = now
                self._schedule_traceback_dump(age)


_watchdog: LivenessWatchdog | None = None


def init_liveness_watchdog(watchdog: LivenessWatchdog) -> LivenessWatchdog:
    global _watchdog
    _watchdog = watchdog
    return watchdog


def get_liveness_watchdog() -> LivenessWatchdog | None:
    return _watchdog


def clear_liveness_watchdog(watchdog: LivenessWatchdog | None = None) -> None:
    """Clear the process singleton without removing a newer replacement."""
    global _watchdog
    if watchdog is None or _watchdog is watchdog:
        _watchdog = None
