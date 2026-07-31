"""PnL milestone tracker API (GET /api/milestones/*).

Read-only cumulative PnL milestone tracking.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.milestone_service import MilestoneService

router = APIRouter(
    prefix="/api/milestones",
    tags=["milestones"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/track")
def track_milestones(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=7, le=3650),
    step: float = Query(default=100.0, ge=10, le=10000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Track cumulative PnL milestone crossings and pace."""
    return MilestoneService(db).track(symbol=symbol, lookback_days=lookback_days, step=step)
