"""Rolling performance metrics API (GET /api/rolling-metrics/*).

Read-only sliding-window analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.rolling_metrics_service import RollingMetricsService

router = APIRouter(
    prefix="/api/rolling-metrics",
    tags=["rolling-metrics"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/compute")
def compute_rolling(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    window: int = Query(default=20, ge=5, le=200),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute rolling Sharpe, win-rate, and PnL over a trade window."""
    return RollingMetricsService(db).compute(
        symbol=symbol, lookback_days=lookback_days, window=window
    )
