"""Profit factor decomposition API (GET /api/profit-factor/*).

Read-only edge decomposition.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.profit_factor_service import ProfitFactorService

router = APIRouter(
    prefix="/api/profit-factor",
    tags=["profit-factor"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/decompose")
def decompose_profit_factor(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Decompose profit factor by symbol, trade size, and concentration."""
    return ProfitFactorService(db).analyze(symbol=symbol, lookback_days=lookback_days)
