"""First-trade-of-day API (GET /api/first-trade/*).

Read-only session-open trade analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.first_trade_service import FirstTradeService

router = APIRouter(
    prefix="/api/first-trade",
    tags=["first-trade"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def first_trade_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compare the first closed trade of each day against the rest."""
    return FirstTradeService(db).summary(days=days)
