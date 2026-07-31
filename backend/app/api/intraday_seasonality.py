"""Intraday seasonality API (GET /api/intraday-seasonality/*).

Read-only time-of-day PnL analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.intraday_seasonality_service import IntradaySeasonalityService

router = APIRouter(
    prefix="/api/intraday-seasonality",
    tags=["intraday-seasonality"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_intraday(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute average PnL by 30-minute intraday bucket."""
    return IntradaySeasonalityService(db).analyze(symbol=symbol, lookback_days=lookback_days)
