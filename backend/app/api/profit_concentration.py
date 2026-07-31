"""Profit concentration API (GET /api/profit-concentration/*).

Read-only Pareto profit concentration analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.profit_concentration_service import ProfitConcentrationService

router = APIRouter(
    prefix="/api/profit-concentration",
    tags=["profit-concentration"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def profit_concentration_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize how concentrated profits are across winning trades."""
    return ProfitConcentrationService(db).summary(days=days)
