from __future__ import annotations

"""Execution quality analytics (GET /api/execution-quality/*). Read-only.

* ``GET /summary`` — fill-rate / rejection-rate / avg-fill-time over a
  trailing window, with a rejection-reason histogram and per-symbol breakdown.
* ``GET /slippage`` — per-symbol average / max slippage from the decision
  (signal) price to the executed price; positive values are favourable.
"""

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.execution_quality_service import ExecutionQualityService

router = APIRouter(
    prefix="/api/execution-quality",
    tags=["execution-quality"],
    dependencies=[Depends(require_api_key())],
)


class SymbolBucket(BaseModel):
    """Per-symbol order/fill/reject counts."""

    orders: int = Field(..., description="All order events for the symbol")
    fills: int = Field(..., description="Fill events for the symbol")
    rejects: int = Field(..., description="Rejection events for the symbol")


class QualitySummary(BaseModel):
    """Aggregate execution-quality statistics for the trailing window."""

    period_days: int
    total_orders: int = Field(..., description="Order-lifecycle events counted")
    filled_orders: int
    rejected_orders: int
    cancelled_orders: int
    fill_rate_pct: float = Field(..., description="filled / total * 100")
    avg_fill_time_seconds: float = Field(..., description="submit->fill latency mean")
    rejection_rate_pct: float = Field(..., description="rejected / total * 100")
    rejection_reasons: dict[str, int] = Field(
        default_factory=dict, description="reason -> count histogram"
    )
    by_symbol: dict[str, SymbolBucket] = Field(
        default_factory=dict, description="per-symbol order/fill/reject counts"
    )


class SlippageRow(BaseModel):
    """Per-symbol slippage statistics."""

    symbol: str
    avg_slippage_pct: float = Field(..., description="Mean slippage as a fraction")
    max_slippage_pct: float = Field(..., description="Worst (most positive) slippage")
    trade_count: int
    direction_bias: str = Field(
        ..., description="'favorable' (mean >= 0) or 'adverse' (mean < 0)"
    )


@router.get("/summary", response_model=QualitySummary)
def quality_summary(
    days: int = Query(default=30, ge=1, le=3650, description="Trailing lookback window in days"),
    db: Session = Depends(get_db),
) -> QualitySummary:
    """Fill / reject / cancel rates and submit->fill latency over the window."""
    summary = ExecutionQualityService(db).get_quality_summary(days=days)
    # Coerce the raw by_symbol dict into the typed SymbolBucket model.
    summary["by_symbol"] = {
        symbol: SymbolBucket.model_validate(bucket)
        for symbol, bucket in summary["by_symbol"].items()
    }
    return QualitySummary.model_validate(summary)


@router.get("/slippage", response_model=list[SlippageRow])
def slippage_analysis(
    days: int = Query(default=30, ge=1, le=3650, description="Trailing lookback window in days"),
    db: Session = Depends(get_db),
) -> list[SlippageRow]:
    """Per-symbol average / max slippage from decision price to fill price."""
    rows = ExecutionQualityService(db).get_slippage_analysis(days=days)
    return [SlippageRow.model_validate(row) for row in rows]
