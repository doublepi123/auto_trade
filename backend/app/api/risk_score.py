"""Composite risk score API (GET /api/risk-score/*).

Read-only multi-factor risk scoring.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.risk_score_service import RiskScoreService

router = APIRouter(
    prefix="/api/risk-score",
    tags=["risk-score"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/compute")
def compute_risk_score(
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute multi-factor composite risk score per symbol."""
    return RiskScoreService(db).compute(lookback_days=lookback_days)
