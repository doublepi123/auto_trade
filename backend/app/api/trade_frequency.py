"""Trade frequency analysis API (GET /api/trade-frequency/*).

Read-only overtrading detection.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.trade_frequency_service import TradeFrequencyService

router = APIRouter(
    prefix="/api/trade-frequency",
    tags=["trade-frequency"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_frequency(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Analyze trade frequency and detect overtrading patterns."""
    return TradeFrequencyService(db).analyze(symbol=symbol, lookback_days=lookback_days)
