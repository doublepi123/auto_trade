"""Benchmark alpha/beta API (GET /api/benchmark/*).

Read-only OLS regression of strategy vs market proxy.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.benchmark_service import BenchmarkService

router = APIRouter(
    prefix="/api/benchmark",
    tags=["benchmark"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/alpha-beta")
def alpha_beta(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Regress strategy PnL against internal market proxy for alpha/beta."""
    return BenchmarkService(db).compute(symbol=symbol, lookback_days=lookback_days)
