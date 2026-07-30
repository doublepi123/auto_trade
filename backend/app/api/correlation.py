"""Symbol correlation API (GET /api/correlation/*).

Read-only pairwise PnL correlation.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.correlation_service import CorrelationService

router = APIRouter(
    prefix="/api/correlation",
    tags=["correlation"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/matrix")
def correlation_matrix(
    lookback_days: int = Query(default=90, ge=7, le=3650),
    min_trades: int = Query(default=3, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute pairwise daily-PnL correlation matrix across symbols."""
    return CorrelationService(db).compute(
        lookback_days=lookback_days, min_trades=min_trades
    )
