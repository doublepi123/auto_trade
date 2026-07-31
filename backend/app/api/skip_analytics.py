"""Skip reason analytics API (GET /api/skip-analytics/*).

Read-only skipped-order analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.skip_analytics_service import SkipAnalyticsService

router = APIRouter(
    prefix="/api/skip-analytics",
    tags=["skip-analytics"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def skip_analytics_summary(
    days: int = Query(default=30, ge=1, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate ORDER_SKIPPED events by skip category."""
    return SkipAnalyticsService(db).summary(days=days)
