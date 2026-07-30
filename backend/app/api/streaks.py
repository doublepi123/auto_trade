"""Trade streak analysis API (GET /api/streaks/*).

Read-only win/loss streak statistics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.streak_service import StreakService

router = APIRouter(
    prefix="/api/streaks",
    tags=["streaks"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_streaks(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute win/loss streak distributions and probabilities."""
    return StreakService(db).analyze(symbol=symbol, lookback_days=lookback_days)
