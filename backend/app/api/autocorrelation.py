"""PnL autocorrelation API (GET /api/autocorrelation/*).

Read-only serial dependence analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.autocorrelation_service import AutocorrelationService

router = APIRouter(
    prefix="/api/autocorrelation",
    tags=["autocorrelation"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_autocorrelation(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    max_lag: int = Query(default=10, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute lag-k autocorrelation and Ljung-Box test on PnL sequence."""
    return AutocorrelationService(db).analyze(
        symbol=symbol, lookback_days=lookback_days, max_lag=max_lag
    )
