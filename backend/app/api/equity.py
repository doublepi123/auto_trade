from __future__ import annotations

from dataclasses import asdict
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.trades import _active_fee_rates
from app.database import get_db
from app.schemas import EquityCurveResponse
from app.services.daily_pnl_service import DailyPnlService
from app.services.equity_curve_service import compute_equity_curve

router = APIRouter(
    prefix="/api/equity",
    tags=["equity"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/curve", response_model=EquityCurveResponse)
def equity_curve(
    symbol: str | None = Query(default=None, description="Optional symbol filter (default: all symbols)"),
    days: int = Query(default=90, ge=1, le=3650, description="Lookback window in days (exit-time based)"),
    db: Session = Depends(get_db),
) -> EquityCurveResponse:
    """Account-wide cumulative realized PnL curve (net, day-granularity).

    Buckets closed round trips by exit day into a cumulative-PnL time series
    with a running peak-to-trough drawdown. Defaults to all symbols and 90 days
    — the always-on equity view that the per-symbol Reports chart and the
    intraday Dashboard PnLChart do not provide. Read-only.
    """
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    from_dt = datetime.now(timezone.utc) - timedelta(days=days)
    trips = DailyPnlService(db).pair_round_trips(
        symbol=symbol,
        from_dt=from_dt,
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
    )
    return EquityCurveResponse.model_validate(asdict(compute_equity_curve(trips)))
