"""Market regime panel API (GET /api/regime/*).

Read-only regime classification for a single symbol, derived from on-ledger
fill prices. No broker calls; returns ``UNKNOWN`` gracefully when there is
insufficient price history.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.regime_service import RegimeService

router = APIRouter(
    prefix="/api/regime",
    tags=["regime"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# response schemas (local to this router per AGENTS.md)
# ----------------------------------------------------------------------


class RegimeIndicatorsSchema(BaseModel):
    volatility_level: str
    trend_direction: str
    volume_regime: str
    price_vs_mean_pct: float


class CurrentRegimeResponse(BaseModel):
    symbol: str
    regime_label: str
    confidence: float = Field(..., ge=0.0, le=1.0)
    indicators: RegimeIndicatorsSchema
    as_of: str
    data_points: int = Field(..., ge=0)


class RegimeHistoryRow(BaseModel):
    date: str
    regime_label: str
    avg_price: float
    volatility_proxy: float


# ----------------------------------------------------------------------
# endpoints (sync handlers per project convention)
# ----------------------------------------------------------------------


@router.get("/current", response_model=CurrentRegimeResponse)
def get_current_regime(
    symbol: str = Query(..., min_length=1, description="Symbol, e.g. AAPL.US"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Current regime classification for ``symbol`` (UNKNOWN when no data)."""
    return RegimeService(db).get_current_regime(symbol)


@router.get("/history", response_model=list[RegimeHistoryRow])
def get_regime_history(
    symbol: str = Query(..., min_length=1, description="Symbol, e.g. AAPL.US"),
    days: int = Query(default=30, ge=1, le=3650, description="Lookback window in days"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    """Daily regime snapshots for ``symbol`` over the lookback window."""
    return RegimeService(db).get_regime_history(symbol, days=days)
