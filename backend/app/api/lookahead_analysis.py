"""Lookahead bias analysis API (GET /api/lookahead-analysis/*).

Read-only analysis that compares trade statistics across chronological
data slices to detect potential future-data leakage.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.lookahead_analysis_service import LookaheadAnalysisService

router = APIRouter(
    prefix="/api/lookahead-analysis",
    tags=["lookahead-analysis"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# response schemas
# ----------------------------------------------------------------------


class BaselineStats(BaseModel):
    trade_count: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0.0, le=1.0)
    total_pnl: float
    avg_pnl: float


class SliceResult(BaseModel):
    pct: float = Field(..., gt=0.0, le=100.0)
    trade_count: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0.0, le=1.0)
    total_pnl: float
    avg_pnl: float
    signal_consistency: float = Field(..., ge=0.0, le=1.0)
    win_rate_delta: float = Field(..., ge=0.0)
    pnl_delta: float = Field(..., ge=0.0)


class LookaheadResponse(BaseModel):
    symbol: str
    lookback_days: int = Field(..., ge=1)
    total_exits: int = Field(..., ge=0)
    baseline: BaselineStats
    slices: list[SliceResult]
    has_bias: bool
    bias_score: float = Field(..., ge=0.0, le=1.0)
    recommendation: str


# ----------------------------------------------------------------------
# endpoints
# ----------------------------------------------------------------------


@router.get("/analyze", response_model=LookaheadResponse)
def analyze_lookahead(
    symbol: str | None = Query(default=None, description="Symbol filter"),
    lookback_days: int = Query(default=90, ge=1, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run lookahead bias analysis across chronological data slices."""
    return LookaheadAnalysisService(db).analyze(
        symbol=symbol, lookback_days=lookback_days
    )
