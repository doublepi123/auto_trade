"""Cron job health API — read-only, authenticated, process-local.

Exposes a safe operational snapshot of the background cron loops created in
``app/main.py``. Each row reports whether a job is enabled, its expected
interval (if known), last success/failure timestamps, a sanitized failure
code (exception class only — never a raw message, order id, or secret), tick
and failure counts, and a staleness verdict.

The endpoint performs no I/O, database, or broker work. ``enabled`` for
DB-gated jobs is reported as ``None`` when it cannot be determined cheaply.
Disabled jobs are never stale. A "success" means the job's existing tick
completed normally; a disabled no-op is not counted as evidence of work.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_api_key
from app.schemas import CronHealthSnapshot, CronJobHealth
from app.services.cron_health_service import get_cron_health_service

router = APIRouter(
    prefix="/api",
    tags=["system"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/cron-health", response_model=CronHealthSnapshot)
def get_cron_health() -> CronHealthSnapshot:
    """Read-only process-local cron job health snapshot (authenticated)."""
    service = get_cron_health_service()
    rows = service.snapshot()
    return CronHealthSnapshot(
        as_of=service.as_of(),
        jobs=[
            CronJobHealth(
                name=row.name,
                enabled=row.enabled,
                expected_interval_seconds=row.expected_interval_seconds,
                last_success_at=row.last_success_at,
                last_failure_at=row.last_failure_at,
                last_failure_code=row.last_failure_code,
                tick_count=row.tick_count,
                failure_count=row.failure_count,
                last_outcome=row.last_outcome,
                stale=row.stale,
                status=row.status,
            )
            for row in rows
        ],
    )