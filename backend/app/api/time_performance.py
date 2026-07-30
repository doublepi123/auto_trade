"""Time-of-day performance API (GET /api/time-performance/*).

Read-only temporal PnL breakdown.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.time_performance_service import TimePerformanceService

router = APIRouter(
    prefix="/api/time-performance",
    tags=["time-performance"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_time_performance(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Break down PnL by hour-of-day and day-of-week."""
    return TimePerformanceService(db).analyze(
        symbol=symbol, lookback_days=lookback_days
    )
