"""Trade tag analytics API (GET /api/tag-analytics/*).

Read-only performance breakdown by trade-note tags.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.tag_analytics_service import TagAnalyticsService

router = APIRouter(
    prefix="/api/tag-analytics",
    tags=["tag-analytics"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/performance")
def tag_performance(
    min_trades: int = Query(default=2, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate trade performance by user-assigned note tags."""
    return TagAnalyticsService(db).analyze(min_trades=min_trades)
