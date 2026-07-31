"""Scratch trade analysis API (GET /api/scratch-analysis/*).

Read-only breakeven-trade analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.scratch_analysis_service import ScratchAnalysisService

router = APIRouter(
    prefix="/api/scratch-analysis",
    tags=["scratch-analysis"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def scratch_analysis_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize scratch trade rate, round-trip fees, and holding time."""
    return ScratchAnalysisService(db).summary(days=days)
