"""Runtime intervention evidence timeline API — read-only, authenticated.

Projects ONLY persisted explicit pause/resume and kill-switch transitions from
``trade_events`` and ``audit_logs``. Never synthesizes runtime-state history,
never infers gaps from ``RuntimeState``, and never mutates any state. See
``InterventionEvidenceService`` for the conservative pairing rule and the
whitelisted transition names.
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import InterventionEvidenceResponse
from app.services.intervention_evidence_service import InterventionEvidenceService
from app.services.snapshot_helper import SnapshotUnavailable

router = APIRouter(
    prefix="/api/intervention-evidence",
    tags=["intervention-evidence"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=InterventionEvidenceResponse)
def list_intervention_evidence(
    from_date: date | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    to_date: date | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    limit: int = Query(
        default=500,
        ge=1,
        le=1000,
        description="Maximum evidence rows to return (bounded)",
    ),
    db=Depends(get_db),
) -> InterventionEvidenceResponse:
    """Read-only runtime intervention evidence timeline."""
    try:
        return InterventionEvidenceService(db).build(
            from_date=from_date,
            to_date=to_date,
            limit=limit,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except SnapshotUnavailable:
        raise HTTPException(
            status_code=503,
            detail=(
                "intervention evidence snapshot unavailable: caller session "
                "cannot be given a distinct physical read snapshot"
            ),
        )
