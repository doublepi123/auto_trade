from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.models import StrategyConfig
from app.schemas import (
    ClosedTrade,
    ClosedTradePage,
    TradeCalendarDay,
    TradeCalendarResponse,
    TradeHoldDurationBucket,
    TradeHoldDurationResponse,
    TradeMonthlySummaryRow,
    TradeMonthlySummaryResponse,
    TradePnlDistributionBucket,
    TradePnlDistributionResponse,
    TradeStats,
    TradeWeekdayAttributionRow,
    TradeWeekdayAttributionResponse,
)
from app.services.daily_pnl_service import DailyPnlService
from app.services.trade_analytics_service import (
    compute_hold_duration_buckets,
    compute_monthly_summary,
    compute_pnl_distribution,
    compute_trade_calendar,
    compute_weekday_attribution,
)
from app.services.trade_stats_service import compute_trade_stats

router = APIRouter(
    prefix="/api/trades",
    tags=["trades"],
    dependencies=[Depends(require_api_key())],
)

_MAX_LIMIT = 500
_DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


def _active_fee_rates(db: Session) -> tuple[float, float]:
    """Read the currently configured fee schedule (US/HK). Falls back to the
    model defaults only when the field is absent/None — an explicit ``0.0``
    (fees disabled) is respected, so it is not collapsed by a falsy-or."""
    config = db.query(StrategyConfig).order_by(StrategyConfig.id.desc()).first()
    fee_us = getattr(config, "fee_rate_us", None)
    fee_hk = getattr(config, "fee_rate_hk", None)
    fee_rate_us = float(fee_us) if fee_us is not None else 0.0005
    fee_rate_hk = float(fee_hk) if fee_hk is not None else 0.003
    return fee_rate_us, fee_rate_hk


def _day_bound(value: str | None, *, end_of_day: bool) -> datetime | None:
    if not value:
        return None
    d = datetime.strptime(value, "%Y-%m-%d").date()
    dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return dt + timedelta(days=1) if end_of_day else dt


def _closed_trips(
    db: Session,
    *,
    symbol: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
):
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    return DailyPnlService(db).pair_round_trips(
        symbol=symbol,
        from_dt=_day_bound(from_date, end_of_day=False),
        to_dt=_day_bound(to_date, end_of_day=True),
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
    )


@router.get("/analytics/calendar", response_model=TradeCalendarResponse)
def trade_calendar(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    db: Session = Depends(get_db),
) -> TradeCalendarResponse:
    trips = _closed_trips(db, symbol=symbol, from_date=from_date, to_date=to_date)
    rows = compute_trade_calendar(trips)
    return TradeCalendarResponse(
        items=[TradeCalendarDay.model_validate(row) for row in rows],
        total_trades=len(trips),
        total_net_pnl=round(sum(trip.net_pnl for trip in trips), 2),
    )


@router.get("/analytics/hold-duration", response_model=TradeHoldDurationResponse)
def trade_hold_duration(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    db: Session = Depends(get_db),
) -> TradeHoldDurationResponse:
    trips = _closed_trips(db, symbol=symbol, from_date=from_date, to_date=to_date)
    return TradeHoldDurationResponse(
        items=[TradeHoldDurationBucket.model_validate(row) for row in compute_hold_duration_buckets(trips)],
        total_trades=len(trips),
    )


@router.get("/analytics/pnl-distribution", response_model=TradePnlDistributionResponse)
def trade_pnl_distribution(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    db: Session = Depends(get_db),
) -> TradePnlDistributionResponse:
    trips = _closed_trips(db, symbol=symbol, from_date=from_date, to_date=to_date)
    return TradePnlDistributionResponse(
        items=[TradePnlDistributionBucket.model_validate(row) for row in compute_pnl_distribution(trips)],
        total_trades=len(trips),
        total_net_pnl=round(sum(trip.net_pnl for trip in trips), 2),
    )


@router.get("/analytics/monthly", response_model=TradeMonthlySummaryResponse)
def trade_monthly_summary(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    db: Session = Depends(get_db),
) -> TradeMonthlySummaryResponse:
    trips = _closed_trips(db, symbol=symbol, from_date=from_date, to_date=to_date)
    rows = compute_monthly_summary(trips)
    return TradeMonthlySummaryResponse(
        items=[TradeMonthlySummaryRow.model_validate(row) for row in rows],
        total_trades=len(trips),
        total_net_pnl=round(sum(trip.net_pnl for trip in trips), 2),
    )


@router.get("/analytics/weekday", response_model=TradeWeekdayAttributionResponse)
def trade_weekday_attribution(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    db: Session = Depends(get_db),
) -> TradeWeekdayAttributionResponse:
    trips = _closed_trips(db, symbol=symbol, from_date=from_date, to_date=to_date)
    return TradeWeekdayAttributionResponse(
        items=[TradeWeekdayAttributionRow.model_validate(row) for row in compute_weekday_attribution(trips)],
        total_trades=len(trips),
        total_net_pnl=round(sum(trip.net_pnl for trip in trips), 2),
    )


@router.get("/stats", response_model=TradeStats)
def trade_stats(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    days: int = Query(default=30, ge=1, le=365, description="Lookback window in days (exit-time based)"),
    db: Session = Depends(get_db),
) -> TradeStats:
    """Per-trade performance stats over closed round trips.

    Sequential run analysis (current/longest win-loss streaks), expectancy,
    profit factor and payoff ratio — the drill-down ``/api/metrics/summary``
    structurally cannot provide. Win/loss is classified on net PnL.
    """
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    from_dt = datetime.now(timezone.utc) - timedelta(days=days)
    trips = DailyPnlService(db).pair_round_trips(
        symbol=symbol,
        from_dt=from_dt,
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
    )
    return TradeStats.model_validate(compute_trade_stats(trips))


@router.get("", response_model=ClosedTradePage)
def list_closed_trades(
    symbol: str | None = Query(default=None, description="Filter by symbol (e.g. AAPL.US)"),
    from_date: str | None = Query(default=None, description="Exit-time lower bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    to_date: str | None = Query(default=None, description="Exit-time upper bound (YYYY-MM-DD)", pattern=_DATE_PATTERN),
    limit: int = Query(default=200, ge=1, le=_MAX_LIMIT, description="Max round trips returned (most-recent first)"),
    db: Session = Depends(get_db),
) -> ClosedTradePage:
    """Closed entry<->exit round trips with realized PnL (lot-level FIFO).

    Read-only drill-down: each row ties a closing fill to the entry lots it
    consumed (weighted-avg entry price, hold duration, persisted fees). Date
    bounds filter on the *exit* time; a round trip that closed inside the window
    is included even if its entry pre-dates it. ``net_pnl`` prefers actual
    broker charges and otherwise uses the fee estimate frozen at submission.
    """
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    trips = DailyPnlService(db).pair_round_trips(
        symbol=symbol,
        from_dt=_day_bound(from_date, end_of_day=False),
        to_dt=_day_bound(to_date, end_of_day=True),
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
    )
    total = len(trips)
    trips = sorted(trips, key=lambda t: t.exit_at, reverse=True)[:limit]
    return ClosedTradePage(
        items=[
            ClosedTrade(
                symbol=t.symbol,
                side=t.side,
                entry_order_id=t.entry_order_id,
                exit_order_id=t.exit_order_id,
                entry_at=t.entry_at,
                exit_at=t.exit_at,
                entry_price=t.entry_price,
                exit_price=t.exit_price,
                quantity=t.quantity,
                gross_pnl=round(t.gross_pnl, 2),
                est_fees=round(t.est_fees, 2),
                net_pnl=round(t.net_pnl, 2),
                holding_seconds=t.holding_seconds,
                fee_source=t.fee_source,
                actual_fees=t.actual_fees,
                slippage_amount=t.slippage_amount,
                slippage_bps=t.slippage_bps,
                ack_latency_ms=t.ack_latency_ms,
                fill_latency_ms=t.fill_latency_ms,
                exit_cause=t.exit_cause,
                exit_reason=t.exit_reason,
                mfe_amount=t.mfe_amount,
                mae_amount=t.mae_amount,
                mfe_pct=t.mfe_pct,
                mae_pct=t.mae_pct,
            )
            for t in trips
        ],
        total=total,
    )
