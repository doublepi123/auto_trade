"""Trade size impact API (GET /api/size-impact/*).

Read-only position-size efficiency analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.size_impact_service import SizeImpactService

router = APIRouter(
    prefix="/api/size-impact",
    tags=["size-impact"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_size_impact(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Analyze PnL efficiency across position size quartiles."""
    return SizeImpactService(db).analyze(symbol=symbol, lookback_days=lookback_days)
