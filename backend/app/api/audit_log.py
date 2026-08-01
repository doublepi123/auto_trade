"""Audit log browse API — read-only, authenticated.

Filters by action/severity/date with bounded limit/offset pagination and
deterministic newest-first ordering. Response schema is a safe projection of
``AuditLog`` (no raw request bodies or secrets).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import AuditLogPage, AuditLogStatsResponse
from app.services.audit_log_service import AuditLogService
from app.services.snapshot_helper import SnapshotUnavailable

router = APIRouter(
    prefix="/api/audit-logs",
    tags=["audit"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=AuditLogPage)
def list_audit_logs(
    action: str | None = Query(default=None, max_length=64, description="Filter by exact action"),
    severity: str | None = Query(default=None, description="Filter by severity (case-insensitive)"),
    from_date: date | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    to_date: date | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(default=100, ge=1, le=500, description="Page size (bounded)"),
    offset: int = Query(default=0, ge=0, description="Row offset"),
    db=Depends(get_db),
) -> AuditLogPage:
    """Read-only audit log browse: newest-first rows, safe filters only."""
    try:
        return AuditLogService(db).list_logs(
            action=action,
            severity=severity,
            from_date=from_date,
            to_date=to_date,
            limit=limit,
            offset=offset,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/stats", response_model=AuditLogStatsResponse)
def audit_log_stats(
    action: str | None = Query(default=None, max_length=64, description="Filter by exact action"),
    severity: str | None = Query(default=None, description="Filter by severity (case-insensitive)"),
    from_date: date | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    to_date: date | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    db=Depends(get_db),
) -> AuditLogStatsResponse:
    """Read-only audit log statistics over the filtered population.

    Reuses the exact ``list_logs`` action/severity/date filter semantics.
    Returns the filtered population total plus deterministic aggregations by
    action, severity, pseudonymous actor hash, and UTC day. Categorical buckets
    are bounded with truthful overflow totals; daily rows are chronological.
    Never exposes raw API keys, IPs, actor material, payload bodies, or
    exception text.
    """
    try:
        return AuditLogService(db).stats(
            action=action,
            severity=severity,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SnapshotUnavailable as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                "audit statistics snapshot unavailable: caller session cannot "
                "be given a distinct physical read snapshot"
            ),
        ) from exc
