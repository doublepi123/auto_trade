"""Process-local scheduler-loop health tracking for background cron jobs.

This module is a deliberately small, dependency-free observer: it records
per-job tick outcomes (success/failure) so the ``/api/cron-health`` endpoint
can report whether each background scheduler loop is making progress. It is
**not** a generic health framework — it knows about the specific cron jobs
created in ``app/main.py`` and nothing else.

Scheduler-loop health semantics (documented explicitly):

* A **tick** is one completed invocation of a cron's existing tick function —
  including a legitimate enabled no-op or "no due work" return. ``tick_count``
  counts completed attempts of either outcome. ``failure_count`` counts the
  subset of ticks that failed. A disabled loop that returns without doing
  work is still a completed tick *of that loop*, but the job's ``enabled``
  flag is reported separately so a disabled no-op is never mistaken for
  enabled work.
* The **latest outcome** controls the health verdict. A first failure is
  ``failing`` (not ``pending``). A success→failure transition is
  ``failing``/unhealthy. A failure→success transition becomes ``healthy``. No
  historical success may mask the latest failure.
* A job is **stale** when it is enabled, has a known expected interval, has
  been activated, and ``monotonic_now - last_tick_at > interval *
  stale_multiplier`` (default 2.0 — one missed tick is a warning, two is
  stale). Staleness uses a monotonic clock so wall-clock jumps cannot affect
  classification.
* A job is **pending** when it has been registered/activated but has not yet
  completed its first tick within the grace period. A job that is registered
  but not yet activated (e.g. during delayed pre-start/import time) is
  ``pending`` and never stale.
* Disabled jobs (``enabled is False``) are explicitly disabled and never
  stale; their ticks are not counted as evidence of enabled work.

Design constraints (P0 live safety):

* Tracking is best-effort and cannot break a cron. Every public mutator
  swallows its own errors (ordinary ``Exception`` only — never
  ``BaseException``/``CancelledError``) and never raises into the caller's
  try/except.
* Instrumentation never alters existing try/except behavior, exception
  propagation/swallowing, loop cadence, task creation, or settings gates.
* No I/O, no DB, no broker calls. ``enabled`` for DB-gated jobs is provided
  by a no-arg callable registered with the job; if the callable raises,
  ``enabled`` is reported as ``None`` (unknown) and the failure is swallowed.
* All mutable job state is copied into an immutable snapshot dataclass
  **while holding the service lock**; classification happens inside the lock
  so no mutable reference escapes.
"""
from __future__ import annotations

import logging
import threading
import time
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


def _monotonic_now() -> float:
    return time.monotonic()


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
    # Monotonic timestamps for staleness (immune to wall-clock jumps).
    last_tick_at: float | None = None
    last_success_at: float | None = None
    last_failure_at: float | None = None
    # Wall-clock timestamps for response display only.
    last_success_at_wall: datetime | None = None
    last_failure_at_wall: datetime | None = None
    last_failure_code: str | None = None
    last_outcome: str = ""  # "success" | "failure" | ""
    tick_count: int = 0
    failure_count: int = 0
    registered_at_mono: float | None = None
    activated_at_mono: float | None = None

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
    """Immutable safe projection of a single job's health for the API.

    All timestamps copied from the mutable record while holding the service
    lock; ``*_wall`` fields are for display only and never used for staleness.
    """

    name: str
    enabled: bool | None
    expected_interval_seconds: float | None
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_failure_code: str | None
    tick_count: int
    failure_count: int
    last_outcome: str
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
        now_monotonic: Callable[[], float] = _monotonic_now,
        now_wall: Callable[[], datetime] = _utc_now,
        stale_multiplier: float = DEFAULT_STALE_MULTIPLIER,
    ) -> None:
        self._now_monotonic = now_monotonic
        self._now_wall = now_wall
        self._stale_multiplier = max(1.0, float(stale_multiplier))
        self._lock = threading.Lock()
        self._jobs: dict[str, JobHealth] = {}

    # --- registration / activation ---------------------------------------

    def register(
        self,
        name: str,
        *,
        expected_interval_seconds: float | None,
        enabled_provider: Callable[[], bool | None] | None = None,
    ) -> None:
        """Register a job (idempotent). Re-registration preserves accumulated
        counters so a re-import of ``app.main`` in tests does not reset state.
        Registration records ``registered_at_mono`` but does NOT activate the
        job — :meth:`activate` marks the job as ready to tick so delayed
        pre-start/import time stays pending rather than stale.
        """
        now_mono = self._now_monotonic()
        with self._lock:
            existing = self._jobs.get(name)
            if existing is not None:
                # Update mutable metadata only; keep counters/timestamps.
                existing.expected_interval_seconds = expected_interval_seconds
                existing.enabled_provider = enabled_provider
                if existing.registered_at_mono is None:
                    existing.registered_at_mono = now_mono
                return
            self._jobs[name] = JobHealth(
                name=name,
                expected_interval_seconds=expected_interval_seconds,
                enabled_provider=enabled_provider,
                registered_at_mono=now_mono,
            )

    def activate(self, name: str) -> None:
        """Mark a registered job as ready to tick (called at startup).

        Idempotent. Best-effort: never raises. A job activated before its
        first tick is ``pending``; staleness is only judged after activation
        so delayed pre-start/import time cannot be reported as stale.
        """
        try:
            now_mono = self._now_monotonic()
            with self._lock:
                job = self._jobs.get(name)
                if job is None:
                    return
                if job.activated_at_mono is None:
                    job.activated_at_mono = now_mono
        except Exception:
            logger.debug("cron-health activate failed", exc_info=True)

    def activate_all(self) -> None:
        """Activate every registered job (best-effort, never raises)."""
        try:
            with self._lock:
                names = list(self._jobs.keys())
        except Exception:
            logger.debug("cron-health activate_all snapshot failed", exc_info=True)
            return
        for name in names:
            self.activate(name)

    def reset(self) -> None:
        """Drop all registered jobs and their counters (tests only)."""
        with self._lock:
            self._jobs.clear()

    # --- mutators (best-effort, never raise; ordinary Exception only) ----

    def record_success(self, name: str) -> None:
        try:
            now_mono = self._now_monotonic()
            now_wall = self._now_wall()
            with self._lock:
                job = self._jobs.get(name)
                if job is None:
                    return
                job.last_tick_at = now_mono
                job.last_success_at = now_mono
                job.last_success_at_wall = now_wall
                job.last_outcome = "success"
                job.tick_count += 1
        except Exception:
            logger.debug("cron-health record_success failed", exc_info=True)

    def record_failure(self, name: str, exc: BaseException) -> None:
        try:
            now_mono = self._now_monotonic()
            now_wall = self._now_wall()
            code = _sanitize_failure_code(exc)
            with self._lock:
                job = self._jobs.get(name)
                if job is None:
                    return
                job.last_tick_at = now_mono
                job.last_failure_at = now_mono
                job.last_failure_at_wall = now_wall
                job.last_failure_code = code
                job.last_outcome = "failure"
                job.tick_count += 1
                job.failure_count += 1
        except Exception:
            logger.debug("cron-health record_failure failed", exc_info=True)

    # --- read projection -------------------------------------------------

    def as_of(self) -> datetime:
        """Return the service's current wall-clock value (for ``as_of``)."""
        return self._now_wall()

    def snapshot(self) -> list[JobHealthSnapshot]:
        """Return a safe, sorted projection of all registered jobs.

        All mutable state is copied into the immutable snapshot dataclass
        while holding the service lock, and classification happens inside the
        lock so no mutable reference escapes.
        """
        now_mono = self._now_monotonic()
        rows: list[JobHealthSnapshot] = []
        with self._lock:
            for job in self._jobs.values():
                enabled = job.is_enabled()
                stale, status = self._classify_locked(job, enabled, now_mono)
                rows.append(
                    JobHealthSnapshot(
                        name=job.name,
                        enabled=enabled,
                        expected_interval_seconds=job.expected_interval_seconds,
                        last_success_at=job.last_success_at_wall,
                        last_failure_at=job.last_failure_at_wall,
                        last_failure_code=job.last_failure_code,
                        tick_count=job.tick_count,
                        failure_count=job.failure_count,
                        last_outcome=job.last_outcome,
                        stale=stale,
                        status=status,
                    )
                )
        rows.sort(key=lambda r: r.name)
        return rows

    def _classify_locked(
        self,
        job: JobHealth,
        enabled: bool | None,
        now_mono: float,
    ) -> tuple[bool, str]:
        """Classify a job as (stale, status). Caller holds the lock.

        Status is one of: ``disabled``, ``healthy``, ``failing``, ``stale``,
        ``pending``, ``unknown``.

        Latest-outcome semantics:
        * A first failure is ``failing`` (not pending).
        * success→failure is ``failing``.
        * failure→success is ``healthy``.
        * No historical success masks the latest failure.
        """
        # Disabled jobs are never stale and not counted as enabled work.
        if enabled is False:
            return False, "disabled"
        interval = job.expected_interval_seconds
        # Latest-outcome verdicts take precedence when we have no interval
        # clock to judge staleness.
        if interval is None or interval <= 0:
            if job.last_outcome == "failure":
                return False, "failing"
            if job.last_outcome == "success":
                return False, "healthy"
            return False, "unknown"
        # Enabled with a known interval. Latest outcome controls health.
        if job.last_outcome == "failure":
            # A failing job is failing regardless of age; staleness is a
            # separate dimension about whether ticks have stopped arriving.
            return self._stale_if_overdue_locked(job, now_mono, interval), "failing"
        if job.last_outcome == "success":
            stale = self._stale_if_overdue_locked(job, now_mono, interval)
            return stale, ("stale" if stale else "healthy")
        # Never ticked yet. Decide pending vs stale by activation time.
        if job.activated_at_mono is not None:
            elapsed_since_activation = now_mono - job.activated_at_mono
            if elapsed_since_activation > interval * self._stale_multiplier:
                return True, "stale"
        # Registered but not activated, or within grace period -> pending.
        return False, "pending"

    def _stale_if_overdue_locked(
        self,
        job: JobHealth,
        now_mono: float,
        interval: float,
    ) -> bool:
        """True when the last tick is older than interval * multiplier."""
        last = job.last_tick_at
        if last is None:
            # No tick yet; fall back to activation time if available.
            if job.activated_at_mono is not None:
                return (now_mono - job.activated_at_mono) > interval * self._stale_multiplier
            return False
        return (now_mono - last) > interval * self._stale_multiplier


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
    """Override the shared service (tests only). Pass ``None`` to reset.

    Restores the prior instance semantics: replacing the singleton with a
    fresh isolated service means later ``get_cron_health_service()`` callers
    see only that service's registrations, so repeated app/TestClient
    lifecycles cannot leave later registries empty or contaminated.
    """
    global _singleton
    with _singleton_lock:
        _singleton = service


def stale_threshold(interval_seconds: float, multiplier: float) -> timedelta:
    """Convenience helper for tests: the staleness threshold as a timedelta."""
    return timedelta(seconds=interval_seconds * max(1.0, float(multiplier)))