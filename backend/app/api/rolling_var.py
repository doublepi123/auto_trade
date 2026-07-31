"""Rolling VaR/CVaR API (GET /api/rolling-var/*).

Read-only risk metrics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.rolling_var_service import RollingVarService

router = APIRouter(
    prefix="/api/rolling-var",
    tags=["rolling-var"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/compute")
def compute_rolling_var(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    window: int = Query(default=30, ge=5, le=200),
    confidence: float = Query(default=0.95, ge=0.8, le=0.99),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute rolling historical VaR and CVaR."""
    return RollingVarService(db).compute(
        symbol=symbol, lookback_days=lookback_days, window=window, confidence=confidence
    )
