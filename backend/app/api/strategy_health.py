from __future__ import annotations

"""Strategy health monitor (GET /api/strategy-health/*). Read-only.

Surfaces live-vs-shadow drift so a human can decide whether the Strategy v2
shadow baseline is tracking the live book. The shadow path never places live
orders and this endpoint never auto-promotes anything — it only reports.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.strategy_health_service import StrategyHealthService

router = APIRouter(
    prefix="/api/strategy-health",
    tags=["strategy-health"],
    dependencies=[Depends(require_api_key())],
)


class TradeSideMetrics(BaseModel):
    """Win-rate / PnL / holding metrics for one side (live or shadow)."""

    win_rate: float = Field(..., description="Fraction of winning trades (0..1)")
    avg_pnl: float = Field(..., description="Mean net PnL per trade")
    trade_count: int = Field(..., description="Number of trades in the window")
    avg_holding_minutes: float = Field(..., description="Mean holding time in minutes")
    profit_factor: float = Field(
        ..., description="Gross profit / gross loss (large value when no losers)"
    )


class HealthDrift(BaseModel):
    """Live-vs-shadow percentage-point drift for the three key metrics."""

    win_rate_drift: float = Field(..., description="live.win_rate - shadow.win_rate")
    pnl_drift: float = Field(..., description="live.avg_pnl - shadow.avg_pnl")
    trade_frequency_drift: float = Field(
        ..., description="(live.trade_count - shadow.trade_count) / shadow.trade_count"
    )


class HealthReport(BaseModel):
    """Single-shot health verdict for the trailing 30-day window."""

    symbol: str | None = Field(default=None, description="Filter applied (None = all symbols)")
    period_days: int = Field(..., description="Fixed 30-day trailing window")
    live_metrics: TradeSideMetrics
    shadow_metrics: TradeSideMetrics
    drift: HealthDrift
    health_status: str = Field(
        ...,
        description="HEALTHY | WARNING | DEGRADED | INSUFFICIENT_DATA",
    )
    alerts: list[str] = Field(
        default_factory=list, description="Human-readable drift warnings"
    )


class TrendRow(BaseModel):
    """One week of live-vs-shadow comparison."""

    week_start: str = Field(..., description="ISO date of the week's Monday")
    live_win_rate: float
    shadow_win_rate: float
    live_avg_pnl: float
    shadow_avg_pnl: float
    live_trades: int
    shadow_trades: int


@router.get("/report", response_model=HealthReport)
def health_report(
    symbol: str | None = Query(
        default=None,
        description="Restrict to one symbol (e.g. AAPL.US). Default: all symbols.",
    ),
    db: Session = Depends(get_db),
) -> HealthReport:
    """Live-vs-shadow drift verdict over the trailing 30-day window."""
    report = StrategyHealthService(db).get_health_report(symbol=symbol)
    return HealthReport.model_validate(report)


@router.get("/trend", response_model=list[TrendRow])
def performance_trend(
    symbol: str | None = Query(
        default=None,
        description="Restrict to one symbol (e.g. AAPL.US). Default: all symbols.",
    ),
    weeks: int = Query(default=8, ge=1, le=52, description="Number of trailing weeks"),
    db: Session = Depends(get_db),
) -> list[TrendRow]:
    """Weekly live-vs-shadow win-rate / avg-PnL / trade-count comparison."""
    rows = StrategyHealthService(db).get_performance_trend(symbol=symbol, weeks=weeks)
    return [TrendRow.model_validate(row) for row in rows]
