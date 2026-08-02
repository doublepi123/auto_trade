"""Durable job lease inspector API — read-only, authenticated.

GET-only endpoint over existing ``DurableJobLease`` rows. Never acquires,
heartbeats, releases, deletes, updates, flushes, or commits leases. Never
exposes raw ``holder_id``.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from app.api.auth import require_api_key
from app.database import get_db
from app.services.durable_job_lease_inspector import DurableJobLeaseInspector

router = APIRouter(
    prefix="/api/durable-job-leases",
    tags=["durable-job-leases"],
    dependencies=[Depends(require_api_key())],
)


@router.get("")
def list_durable_job_leases(db=Depends(get_db)) -> dict[str, Any]:
    """Read-only durable job lease inspector.

    Classifies leases using SQLite's clock: ``expires_at > now`` is ACTIVE;
    equality is RECLAIMABLE. Returns observation time, lease key, fencing
    token, timestamps, status, and a hashed holder fingerprint. Never exposes
    raw ``holder_id``; never mutates any lease or session state.
    """
    return DurableJobLeaseInspector(db).inspect()
