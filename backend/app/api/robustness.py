"""Strategy robustness API (GET /api/robustness/*).

Read-only composite robustness scoring.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.robustness_service import RobustnessService

router = APIRouter(
    prefix="/api/robustness",
    tags=["robustness"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/score")
def robustness_score(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=30, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute composite strategy robustness index (0-100)."""
    return RobustnessService(db).score(symbol=symbol, lookback_days=lookback_days)
