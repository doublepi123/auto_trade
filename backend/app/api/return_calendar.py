"""Return calendar API (GET /api/return-calendar/*).

Read-only weekly/monthly PnL aggregation.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.return_calendar_service import ReturnCalendarService

router = APIRouter(
    prefix="/api/return-calendar",
    tags=["return-calendar"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/compute")
def compute_calendar(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate PnL into ISO-week and calendar-month buckets."""
    return ReturnCalendarService(db).compute(symbol=symbol, lookback_days=lookback_days)
