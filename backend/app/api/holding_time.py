"""Holding time analysis API (GET /api/holding-time/*).

Read-only PnL breakdown by trade duration.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.holding_time_service import HoldingTimeService

router = APIRouter(
    prefix="/api/holding-time",
    tags=["holding-time"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_holding_time(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Break down PnL by trade holding duration buckets."""
    return HoldingTimeService(db).analyze(symbol=symbol, lookback_days=lookback_days)
