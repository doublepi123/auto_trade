"""Prior-outcome variability API (GET /api/regime-sensitivity/*).

Read-only analysis of performance conditioned on prior closed-trade PnL
variability. This is not a market-volatility signal. Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.regime_sensitivity_service import RegimeSensitivityService

router = APIRouter(
    prefix="/api/regime-sensitivity",
    tags=["regime-sensitivity"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_regime(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    window: int = Query(default=20, ge=5, le=100),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compare outcomes across prior closed-trade PnL variability states."""
    return RegimeSensitivityService(db).analyze(
        symbol=symbol, lookback_days=lookback_days, window=window
    )
