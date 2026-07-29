from __future__ import annotations

"""Read-only drawdown-analysis panel.

Layers a peak-to-trough drawdown view on top of the canonical closed-round-trip
ledger exposed by :class:`DailyPnlService`. Two endpoints:

* ``GET /api/drawdown-analysis/summary`` — aggregate stats (current/max
  drawdown, depth, duration, recovery behaviour) for a trailing window.
* ``GET /api/drawdown-analysis/timeline`` — per-trade equity-curve snapshots
  with running peak and drawdown.

Both endpoints are read-only and honour an optional ``symbol`` filter and a
``days`` lookback window (exit-time based, same convention as ``/api/trades``).
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.drawdown_analysis_service import DrawdownAnalysisService

router = APIRouter(
    prefix="/api/drawdown-analysis",
    tags=["drawdown-analysis"],
    dependencies=[Depends(require_api_key())],
)


class DrawdownSummary(BaseModel):
    """Aggregate drawdown statistics for one symbol/window."""

    symbol: str | None = Field(default=None, description="Filter applied (None = all symbols)")
    period_days: int = Field(..., description="Trailing lookback window in days")
    peak_pnl: float = Field(..., description="Highest cumulative net PnL reached in-window")
    current_pnl: float = Field(..., description="Cumulative net PnL at the latest in-window trip")
    current_drawdown: float = Field(..., description="current_pnl - peak_pnl (<= 0)")
    current_drawdown_pct: float = Field(
        ..., description="current_drawdown as a fraction of peak_pnl (0 when no peak)"
    )
    max_drawdown: float = Field(..., description="Deepest drawdown reached in-window")
    max_drawdown_pct: float = Field(..., description="max_drawdown as a fraction of its peak")
    max_drawdown_date: str | None = Field(
        default=None, description="ISO timestamp when max_drawdown was reached"
    )
    max_drawdown_duration_days: int = Field(..., description="Longest drawdown span in days")
    recovery_count: int = Field(..., description="Completed drawdown->peak recoveries in-window")
    avg_recovery_days: float = Field(..., description="Mean recovery time across completed recoveries")
    is_in_drawdown: bool = Field(..., description="True when current_pnl sits below the running peak")


class DrawdownTimelinePoint(BaseModel):
    """One per-trade equity-curve snapshot."""

    date: str = Field(..., description="ISO timestamp of the round-trip exit")
    timestamp: str = Field(..., description="ISO timestamp (alias of date)")
    cumulative_pnl: float = Field(..., description="Cumulative net PnL through this trip")
    peak_pnl: float = Field(..., description="Running peak PnL up to and including this trip")
    drawdown: float = Field(..., description="cumulative_pnl - peak_pnl")
    drawdown_pct: float = Field(..., description="drawdown as a fraction of peak_pnl")
    is_in_drawdown: bool = Field(..., description="True when this point sits below the peak")


@router.get("/summary", response_model=DrawdownSummary)
def drawdown_summary(
    symbol: str | None = Query(
        default=None, description="Restrict to one symbol (e.g. AAPL.US). Default: all symbols."
    ),
    days: int = Query(default=90, ge=1, le=3650, description="Trailing lookback window in days"),
    db: Session = Depends(get_db),
) -> DrawdownSummary:
    """Aggregate peak-to-trough drawdown statistics over the trailing window."""
    return DrawdownSummary.model_validate(
        DrawdownAnalysisService(db).get_drawdown_summary(symbol=symbol, days=days)
    )


@router.get("/timeline", response_model=list[DrawdownTimelinePoint])
def drawdown_timeline(
    symbol: str | None = Query(
        default=None, description="Restrict to one symbol (e.g. AAPL.US). Default: all symbols."
    ),
    days: int = Query(default=90, ge=1, le=3650, description="Trailing lookback window in days"),
    db: Session = Depends(get_db),
) -> list[DrawdownTimelinePoint]:
    """Per-trade equity-curve snapshots (chronological, by exit time)."""
    return [
        DrawdownTimelinePoint.model_validate(point)
        for point in DrawdownAnalysisService(db).get_drawdown_timeline(symbol=symbol, days=days)
    ]
