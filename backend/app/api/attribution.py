"""Performance attribution API (GET /api/attribution/*).

Read-only decomposition of realized PnL across symbols, directions, exit
reasons, market sessions and calendar days. Sourced entirely from the
``OrderRecord`` exit ledger; never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.performance_attribution_service import PerformanceAttributionService

router = APIRouter(
    prefix="/api/attribution",
    tags=["attribution"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# response schemas (local to this router per AGENTS.md)
# ----------------------------------------------------------------------


class SymbolBucket(BaseModel):
    total_pnl: float
    trade_count: int
    win_count: int
    avg_pnl: float


class DirectionBucket(BaseModel):
    total_pnl: float
    trade_count: int


class ExitReasonBucket(BaseModel):
    total_pnl: float
    trade_count: int


class SessionBucket(BaseModel):
    total_pnl: float
    trade_count: int


class DayBucket(BaseModel):
    date: str
    pnl: float
    trade_count: int


class AttributionResponse(BaseModel):
    period_days: int = Field(..., ge=0)
    total_pnl: float
    total_trades: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0.0, le=1.0)
    by_symbol: dict[str, SymbolBucket]
    by_direction: dict[str, DirectionBucket]
    by_exit_reason: dict[str, ExitReasonBucket]
    by_session: dict[str, SessionBucket]
    by_day: list[DayBucket]


class TopContributorRow(BaseModel):
    symbol: str
    total_pnl: float
    trade_count: int = Field(..., ge=0)
    win_rate: float = Field(..., ge=0.0, le=1.0)
    avg_holding_minutes: float = Field(..., ge=0.0)


# ----------------------------------------------------------------------
# endpoints (sync handlers per project convention)
# ----------------------------------------------------------------------


@router.get("/pnl", response_model=AttributionResponse)
def get_pnl_attribution(
    days: int = Query(default=30, ge=1, le=3650, description="Lookback window in days"),
    symbol: str | None = Query(
        default=None,
        description="Restrict to a single symbol (exact match, e.g. AAPL.US)",
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Multi-dimensional realized-PnL breakdown for the lookback window."""
    return PerformanceAttributionService(db).attribute_pnl(days=days, symbol=symbol)


@router.get("/top-contributors", response_model=list[TopContributorRow])
def get_top_contributors(
    days: int = Query(default=30, ge=1, le=3650, description="Lookback window in days"),
    limit: int = Query(default=10, ge=1, le=200, description="Maximum rows to return"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Top symbols by absolute PnL contribution, with holding-time stats."""
    return PerformanceAttributionService(db).top_contributors(days=days, limit=limit)
