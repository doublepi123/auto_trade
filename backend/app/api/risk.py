from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import RiskHistoryResponse
from app.services.risk_history_service import RiskHistoryService

router = APIRouter(
    prefix="/api/risk",
    tags=["risk"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/history", response_model=RiskHistoryResponse)
def get_risk_history(
    symbol: Optional[str] = Query(default=None, max_length=50),
    limit: int = Query(default=100, ge=1, le=500),
    db=Depends(get_db),
) -> RiskHistoryResponse:
    """Chronological risk-state snapshots (daily PnL, consecutive losses,
    paused / kill-switch). Read-only."""
    return RiskHistoryService(db).get_history(symbol=symbol, limit=limit)
