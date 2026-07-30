"""Recovery timeline API (GET /api/recovery/*).

Read-only drawdown episode and recovery-time analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.recovery_service import RecoveryService

router = APIRouter(
    prefix="/api/recovery",
    tags=["recovery"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/timeline")
def recovery_timeline(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=365, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Detect drawdown episodes and measure recovery time."""
    return RecoveryService(db).analyze(symbol=symbol, lookback_days=lookback_days)
