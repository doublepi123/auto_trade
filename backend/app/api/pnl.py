from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.trades import _active_fee_rates
from app.database import get_db
from app.schemas import SymbolAttributionResponse
from app.services.daily_pnl_service import DailyPnlService
from app.services.statistics_quality_service import select_statistics_sample
from app.services.symbol_attribution_service import compute_symbol_attribution

router = APIRouter(
    prefix="/api/pnl",
    tags=["pnl"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/by-symbol", response_model=SymbolAttributionResponse)
def pnl_by_symbol(
    symbol: str | None = Query(default=None, description="Optional single-symbol filter (default: all symbols)"),
    days: int = Query(default=30, ge=1, le=3650, description="Lookback window in days (exit-time based)"),
    db: Session = Depends(get_db),
) -> SymbolAttributionResponse:
    """Portfolio-level realized PnL grouped by symbol.

    Winners/losers with win-rate, contribution share and best/worst trade — the
    cross-symbol axis that the single-symbol, side-keyed ReportService
    attribution cannot provide. Defaults to all symbols and 30 days. Read-only.
    """
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    from_dt = datetime.now(timezone.utc) - timedelta(days=days)
    replay = DailyPnlService(db).pair_round_trips_with_issues(
        symbol=symbol,
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
    )
    sample = select_statistics_sample(replay, from_dt=from_dt)
    payload = asdict(compute_symbol_attribution(sample.trades))
    payload["statistics_quality"] = asdict(sample.quality)
    return SymbolAttributionResponse.model_validate(payload)
