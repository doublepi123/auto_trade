"""Window-local underwater duration API (GET /api/drawdown-duration/*).

Read-only completed-recovery analytics with boundary and open runs reported
as censored. The pre-window high-water mark is not available. Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.drawdown_duration_service import DrawdownDurationService

router = APIRouter(
    prefix="/api/drawdown-duration",
    tags=["drawdown-duration"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_duration(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Analyze fully observed window-local recovery durations."""
    return DrawdownDurationService(db).analyze(symbol=symbol, lookback_days=lookback_days)
