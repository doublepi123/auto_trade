"""Win/loss asymmetry API (GET /api/asymmetry/*).

Read-only distribution asymmetry analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.asymmetry_service import AsymmetryService

router = APIRouter(
    prefix="/api/asymmetry",
    tags=["asymmetry"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_asymmetry(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Analyze win/loss distribution asymmetry and conditional patterns."""
    return AsymmetryService(db).analyze(symbol=symbol, lookback_days=lookback_days)
