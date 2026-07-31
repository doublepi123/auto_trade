"""Re-entry behavior API (GET /api/reentry-analysis/*).

Read-only sequential trade analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.reentry_analysis_service import ReentryAnalysisService

router = APIRouter(
    prefix="/api/reentry-analysis",
    tags=["reentry-analysis"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def reentry_analysis_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Outcome of trades conditioned on the previous same-symbol trade."""
    return ReentryAnalysisService(db).summary(days=days)
