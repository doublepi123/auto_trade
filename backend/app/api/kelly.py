"""Kelly criterion API (GET /api/kelly/*).

Read-only position sizing estimates.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.kelly_service import KellyService

router = APIRouter(
    prefix="/api/kelly",
    tags=["kelly"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/sizing")
def kelly_sizing(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Compute Kelly criterion position sizing from historical trades."""
    return KellyService(db).compute(symbol=symbol, lookback_days=lookback_days)
