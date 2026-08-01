"""Process-local health tracking for background cron loops.

This module is a deliberately small, dependency-free observer: it records
per-job success/failure timestamps and tick/failure counts so the
``/api/cron-health`` endpoint can report whether each background loop is
making progress. It is **not** a generic health framework — it knows about
the specific cron jobs created in ``app/main.py`` and nothing else.

Design constraints (P0 live safety):

* Tracking is best-effort and cannot break a cron. Every public mutator
  swallows its own errors and never raises into the caller's try/except.
* Instrumentation never alters existing try/except behavior, exception
  propagation/swallowing, loop cadence, task creation, or settings gates.
  Callers record success/failure *after* their existing work completes;
  the tracker does not wrap or intercept the work.
* A "success" means the job's existing tick completed normally. A disabled
  no-op tick is *not* counted as evidence of enabled work — disabled jobs
  record nothing and are never stale.
* No I/O, no DB, no broker calls. ``enabled`` for DB-gated jobs is provided
  by a no-arg callable registered with the job; if the callable itself is
  unavailable or raises, ``enabled`` is reported as ``None`` (unknown) and
  the failure is swallowed.

Clock/staleness semantics:

* ``now`` is injected (default ``datetime.now(timezone.utc)``) so tests use a
  deterministic fake clock.
* A job is **stale** when it is enabled, has a known expected interval, and
  ``now - last_success_at > expected_interval_seconds * stale_multiplier``
  (default multiplier 2.0 — one missed tick is a warning, two is stale).
* A job that has never succeeded but is enabled is stale only if it has a
  known expected interval and enough wall-clock has elapsed since process
  start to expect at least one tick; otherwise it is ``pending`` (newly
  started, no heartbeat yet).
* Disabled jobs are never stale.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Final

logger = logging.getLogger("auto_trade.cron_health")

#: Multiplier applied to the expected interval before a job is considered
#: stale. One missed tick (interval < elapsed <= 2*interval) is a warning;
#: two missed ticks (elapsed > 2*interval) is stale.
DEFAULT_STALE_MULTIPLIER: Final[float] = 2.0

#: Maximum length of the sanitized failure code string. Exception class names
#: are truncated to this length so a pathological exception type cannot bloat
#: the process-local state.
_MAX_FAILURE_CODE_LEN: Final[int] = 120


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _sanitize_failure_code(exc: BaseException) -> str:
    """Return a safe, non-leaking category for a cron failure.

    Only the exception class name is exposed — never the message, which may
    contain secrets, order ids, or credential material. ``None``/unknown
    exceptions collapse to ``"Unknown"``.
    """
    name = type(exc).__name__ or "Unknown"
    if len(name) > _MAX_FAILURE_CODE_LEN:
        name = name[:_MAX_FAILURE_CODE_LEN]
    return name or "Unknown"


@dataclass
class JobHealth:
    """Mutable per-job health record (process-local, lock-protected)."""

    name: str
    expected_interval_seconds: float | None
    enabled_provider: Callable[[], bool | None] | None
    last_success_at: datetime | None = None
    last_failure_at: datetime | None = None
    last_failure_code: str | None = None
    tick_count: int = 0
    failure_count: int = 0
    registered_at: datetime | None = None

    def is_enabled(self) -> bool | None:
        provider = self.enabled_provider
        if provider is None:
            return None
        try:
            value = provider()
        except Exception:
            logger.debug(
                "cron-health enabled_provider for %s raised; reporting None",
                self.name,
                exc_info=True,
            )
            return None
        # Preserve None explicitly; only coerce actual booleans.
        if value is None:
            return None
        return bool(value)


@dataclass(frozen=True)
class JobHealthSnapshot:
    """Immutable safe projection of a single job's health for the API."""

    name: str
    enabled: bool | None
    expected_interval_seconds: float | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_code: str | None
    tick_count: int
    failure_count: int
    stale: bool
    status: str


class CronHealthService:
    """Process-local registry of per-job health records.

    A single shared instance (``get_cron_health_service()``) is used by the
    cron loops in ``app/main.py`` and read by the ``/api/cron-health`` API.
    All mutation and reads are guarded by a single lock so concurrent cron
    ticks and API reads cannot tear the state.
    """

    def __init__(
        self,
        *,
        now: Callable[[], datetime] = _utc_now,
        stale_multiplier: float = DEFAULT_STALE_MULTIPLIER,
    ) -> None:
        self._now = now
        self._stale_multiplier = max(1.0, float(stale_multiplier))
        self._lock = threading.Lock()
        self._jobs: dict[str, JobHealth] = {}

    # --- registration ----------------------------------------------------

    def register(
        self,
        name: str,
        *,
        expected_interval_seconds: float | None,
        enabled_provider: Callable[[], bool | None] | None = None,
    ) -> None:
        """Register a job (idempotent). Re-registration preserves accumulated
        counters so a re-import of ``app.main`` in tests does not reset state.
        """
        now = self._now()
        with self._lock:
            existing = self._jobs.get(name)
            if existing is not None:
                # Update mutable metadata only; keep counters/timestamps.
                existing.expected_interval_seconds = expected_interval_seconds
                existing.enabled_provider = enabled_provider
                # Ensure registered_at is set (older instances may predate it).
                if existing.registered_at is None:
                    existing.registered_at = now
                return
            self._jobs[name] = JobHealth(
                name=name,
                expected_interval_seconds=expected_interval_seconds,
                enabled_provider=enabled_provider,
                registered_at=now,
            )

    def reset(self) -> None:
        """Drop all registered jobs and their counters (tests only)."""
        with self._lock:
            self._jobs.clear()

    # --- mutators (best-effort, never raise) -----------------------------

    def record_success(self, name: str) -> None:
        try:
            now = self._now()
            with self._lock:
                job = self._jobs.get(name)
                if job is None:
                    return
                job.last_success_at = now
                job.tick_count += 1
        except Exception:
            logger.debug("cron-health record_success failed", exc_info=True)

    def record_failure(self, name: str, exc: BaseException) -> None:
        try:
            now = self._now()
            code = _sanitize_failure_code(exc)
            with self._lock:
                job = self._jobs.get(name)
                if job is None:
                    return
                job.last_failure_at = now
                job.last_failure_code = code
                job.failure_count += 1
        except Exception:
            logger.debug("cron-health record_failure failed", exc_info=True)

    # --- read projection -------------------------------------------------

    def as_of(self) -> datetime:
        """Return the service's current clock value (for response ``as_of``)."""
        return self._now()

    def snapshot(self) -> list[JobHealthSnapshot]:
        """Return a safe, sorted projection of all registered jobs."""
        now = self._now()
        with self._lock:
            jobs = list(self._jobs.values())
        rows: list[JobHealthSnapshot] = []
        for job in jobs:
            enabled = job.is_enabled()
            stale, status = self._classify(job, enabled, now)
            rows.append(
                JobHealthSnapshot(
                    name=job.name,
                    enabled=enabled,
                    expected_interval_seconds=job.expected_interval_seconds,
                    last_success_at=job.last_success_at,
                    last_failure_at=job.last_failure_at,
                    last_failure_code=job.last_failure_code,
                    tick_count=job.tick_count,
                    failure_count=job.failure_count,
                    stale=stale,
                    status=status,
                )
            )
        rows.sort(key=lambda r: r.name)
        return rows

    def _classify(
        self,
        job: JobHealth,
        enabled: bool | None,
        now: datetime,
    ) -> tuple[bool, str]:
        """Classify a job as (stale, status).

        Status is one of: ``disabled``, ``healthy``, ``stale``, ``pending``,
        ``failing``, ``unknown``.
        """
        # Disabled jobs are never stale.
        if enabled is False:
            return False, "disabled"
        interval = job.expected_interval_seconds
        # Without an expected interval we cannot judge staleness.
        if interval is None or interval <= 0:
            # If it has succeeded at least once and is enabled, call it healthy;
            # otherwise we have no clock to judge by.
            if job.last_success_at is not None:
                return False, "healthy"
            if job.last_failure_at is not None and (
                job.last_success_at is None
            ):
                return False, "failing"
            return False, "unknown"
        # Enabled with a known interval.
        if job.last_success_at is not None:
            elapsed = (now - job.last_success_at).total_seconds()
            threshold = interval * self._stale_multiplier
            if elapsed > threshold:
                return True, "stale"
            return False, "healthy"
        # Enabled, never succeeded yet. Decide pending vs stale by checking
        # whether enough wall-clock has elapsed since registration to expect
        # at least one tick.
        if job.registered_at is not None:
            elapsed_since_register = (now - job.registered_at).total_seconds()
            if elapsed_since_register > interval * self._stale_multiplier:
                return True, "stale"
        return False, "pending"


# --- module singleton ----------------------------------------------------

_singleton: CronHealthService | None = None
_singleton_lock = threading.Lock()


def get_cron_health_service() -> CronHealthService:
    """Return the process-local shared CronHealthService.

    Tests can replace it via ``set_cron_health_service`` for deterministic
    fake-clock isolation.
    """
    global _singleton
    with _singleton_lock:
        if _singleton is None:
            _singleton = CronHealthService()
        return _singleton


def set_cron_health_service(service: CronHealthService | None) -> None:
    """Override the shared service (tests only). Pass ``None`` to reset."""
    global _singleton
    with _singleton_lock:
        _singleton = service


def stale_threshold(interval_seconds: float, multiplier: float) -> timedelta:
    """Convenience helper for tests: the staleness threshold as a timedelta."""
    return timedelta(seconds=interval_seconds * max(1.0, float(multiplier)))