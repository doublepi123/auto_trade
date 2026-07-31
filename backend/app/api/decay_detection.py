"""Strategy decay detection API (GET /api/decay-detection/*).

Read-only edge degradation analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.decay_detection_service import DecayDetectionService

router = APIRouter(
    prefix="/api/decay-detection",
    tags=["decay-detection"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/detect")
def detect_decay(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=30, le=3650),
    n_windows: int = Query(default=4, ge=2, le=10),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Detect strategy edge decay across sequential time windows."""
    return DecayDetectionService(db).detect(
        symbol=symbol, lookback_days=lookback_days, n_windows=n_windows
    )
