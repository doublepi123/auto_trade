"""Daily PnL consistency API (GET /api/daily-consistency/*).

Read-only day-level consistency analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.daily_consistency_service import DailyConsistencyService

router = APIRouter(
    prefix="/api/daily-consistency",
    tags=["daily-consistency"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def daily_consistency_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize day-level PnL consistency metrics."""
    return DailyConsistencyService(db).summary(days=days)
