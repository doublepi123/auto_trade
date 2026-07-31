"""Symbol concentration API (GET /api/concentration/*).

Read-only HHI / effective-N analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.concentration_service import ConcentrationService

router = APIRouter(
    prefix="/api/concentration",
    tags=["concentration"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_concentration(
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute HHI and effective-N concentration metrics."""
    return ConcentrationService(db).analyze(lookback_days=lookback_days)
