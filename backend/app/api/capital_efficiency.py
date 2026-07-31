"""Capital efficiency API (GET /api/capital-efficiency/*).

Read-only capital utilization analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.capital_efficiency_service import CapitalEfficiencyService

router = APIRouter(
    prefix="/api/capital-efficiency",
    tags=["capital-efficiency"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_efficiency(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    capital_base: float = Query(default=10000.0, ge=100, le=10000000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute return-on-capital, turnover, and utilization metrics."""
    return CapitalEfficiencyService(db).analyze(
        symbol=symbol, lookback_days=lookback_days, capital_base=capital_base
    )
