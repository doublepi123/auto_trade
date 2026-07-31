"""PnL distribution shape API (GET /api/distribution-shape/*).

Read-only statistical shape analysis.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.distribution_shape_service import DistributionShapeService

router = APIRouter(
    prefix="/api/distribution-shape",
    tags=["distribution-shape"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_distribution(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute skewness, kurtosis, and normality of PnL distribution."""
    return DistributionShapeService(db).analyze(symbol=symbol, lookback_days=lookback_days)
