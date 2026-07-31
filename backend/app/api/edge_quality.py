"""Edge quality score API (GET /api/edge-quality/*).

Read-only composite strategy quality scoring.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.edge_quality_service import EdgeQualityService

router = APIRouter(
    prefix="/api/edge-quality",
    tags=["edge-quality"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/score")
def edge_quality_score(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute composite edge quality score (0-100) with letter grade."""
    return EdgeQualityService(db).score(symbol=symbol, lookback_days=lookback_days)
