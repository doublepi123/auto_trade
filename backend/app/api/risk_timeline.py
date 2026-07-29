from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.risk_timeline_service import RiskTimelineService


router = APIRouter(
    prefix="/api/risk-timeline",
    tags=["risk-timeline"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# Response schemas (kept local to avoid schemas.py merge conflicts)
# ----------------------------------------------------------------------
class RiskCheckOut(BaseModel):
    id: int
    event_type: str
    trade_id: str | None = None
    symbol: str = ""
    side: str = ""
    status: str = ""
    passed: bool
    check_name: str = ""
    reason: str = ""
    skip_category: str = ""
    threshold: float | None = None
    actual_value: float | None = None
    created_at: str | None = None


class RiskSummaryOut(BaseModel):
    window_hours: int
    total_checks: int = 0
    passed: int = 0
    blocked: int = 0
    by_category: dict[str, int] = Field(default_factory=dict)
    recent_blocks: list[RiskCheckOut] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Routes (sync handlers, per repo convention)
# ----------------------------------------------------------------------
@router.get("/checks", response_model=list[RiskCheckOut])
def get_checks(
    trade_id: int | None = Query(
        default=None,
        description="Broker order id (one round trip's events)",
        ge=1,
    ),
    symbol: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return RiskTimelineService(db).get_trade_risk_checks(
        trade_id=trade_id, symbol=symbol, limit=limit,
    )


@router.get("/summary", response_model=RiskSummaryOut)
def get_summary(
    hours: int = Query(default=24, ge=0, le=720),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return RiskTimelineService(db).get_risk_summary(hours=hours)
