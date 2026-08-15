from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.models import ReconciliationEvidence, RuntimeState
from app.runner import get_runner
from app.schemas import (
    ReconciliationBrokerSnapshot,
    ReconciliationEvidenceSchema,
    ReconciliationEvidenceSurfaceResponse,
    ReconciliationStatusResponse,
)

logger = logging.getLogger("auto_trade.reconciliation")

router = APIRouter(prefix="/api/reconciliation", tags=["reconciliation"])


@router.get(
    "/status",
    response_model=ReconciliationStatusResponse,
    dependencies=[Depends(require_api_key())],
)
def get_reconciliation_status(db: Session = Depends(get_db)) -> ReconciliationStatusResponse:
    """Return the current reconciliation gate status and evidence records.

    Returns:
        - reconciliation_gate: current gate state (pending/passed/failed)
        - last_evidence: the most recent reconciliation evidence record
        - recent_evidence: the last 20 evidence records
        - force_resume_available: whether force-resume is supported
    """
    # Read the current reconciliation gate from the runtime state
    runtime_state = db.query(RuntimeState).first()
    gate = getattr(runtime_state, "reconciliation_gate", "pending") if runtime_state else "pending"

    # Latest evidence record
    last_evidence_row = (
        db.query(ReconciliationEvidence)
        .order_by(ReconciliationEvidence.timestamp.desc(), ReconciliationEvidence.id.desc())
        .first()
    )
    last_evidence = (
        ReconciliationEvidenceSchema.model_validate(last_evidence_row)
        if last_evidence_row is not None
        else None
    )

    # Last 20 evidence records
    recent_rows = (
        db.query(ReconciliationEvidence)
        .order_by(ReconciliationEvidence.timestamp.desc(), ReconciliationEvidence.id.desc())
        .limit(20)
        .all()
    )
    recent_evidence = [
        ReconciliationEvidenceSchema.model_validate(row) for row in recent_rows
    ]

    # Check if force_resume is available
    runner = get_runner()
    force_resume_available = getattr(runner, "force_resume_reconciliation_gate", None) is not None

    return ReconciliationStatusResponse(
        reconciliation_gate=gate,
        last_evidence=last_evidence,
        recent_evidence=recent_evidence,
        force_resume_available=force_resume_available,
    )


@router.get(
    "/evidence-surface",
    response_model=ReconciliationEvidenceSurfaceResponse,
    dependencies=[Depends(require_api_key())],
)
def get_evidence_surface(db: Session = Depends(get_db)) -> ReconciliationEvidenceSurfaceResponse:
    """Return the full operator evidence surface including broker snapshot freshness.

    Combines reconciliation gate status with broker snapshot health metrics
    so operators can verify the system state at a glance.
    """
    # Gate status
    runtime_state = db.query(RuntimeState).first()
    gate = getattr(runtime_state, "reconciliation_gate", "pending") if runtime_state else "pending"

    last_evidence_row = (
        db.query(ReconciliationEvidence)
        .order_by(ReconciliationEvidence.timestamp.desc(), ReconciliationEvidence.id.desc())
        .first()
    )
    last_evidence = (
        ReconciliationEvidenceSchema.model_validate(last_evidence_row)
        if last_evidence_row is not None
        else None
    )

    recent_rows = (
        db.query(ReconciliationEvidence)
        .order_by(ReconciliationEvidence.timestamp.desc(), ReconciliationEvidence.id.desc())
        .limit(20)
        .all()
    )
    recent_evidence = [
        ReconciliationEvidenceSchema.model_validate(row) for row in recent_rows
    ]

    runner = get_runner()
    force_resume_available = getattr(runner, "force_resume_reconciliation_gate", None) is not None

    gate_response = ReconciliationStatusResponse(
        reconciliation_gate=gate,
        last_evidence=last_evidence,
        recent_evidence=recent_evidence,
        force_resume_available=force_resume_available,
    )

    # Broker snapshot
    broker_connected = bool(getattr(runner, "broker", None))
    broker_snapshot = ReconciliationBrokerSnapshot(
        broker_connected=broker_connected,
        last_sync_age_seconds=None,
        position_count=last_evidence_row.position_count if last_evidence_row else None,
        order_count=last_evidence_row.order_count if last_evidence_row else None,
        order_certainty="certain" if gate == "passed" else "uncertain",
        position_certainty="certain" if gate == "passed" else "uncertain",
    )

    return ReconciliationEvidenceSurfaceResponse(
        gate=gate_response,
        broker_snapshot=broker_snapshot,
    )