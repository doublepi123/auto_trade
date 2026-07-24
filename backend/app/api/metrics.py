from __future__ import annotations

from dataclasses import asdict
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.trades import _active_fee_rates
from app.core.market_calendar import trade_day_for
from app.database import get_db
from app.schemas import MetricsSummaryResponse
from app.services.daily_pnl_service import ClosedRoundTrip, DailyPnlService
from app.services.statistics_quality_service import select_statistics_sample

router = APIRouter(prefix="/api/metrics", tags=["metrics"])


def _compute_sharpe(daily_pnls: list[float]) -> Optional[float]:
    """Annualized realized-PnL Sharpe from weekday observations.

    Absolute currency scale cancels in the ratio. This remains a realized-PnL
    statistic rather than a mark-to-market account-return Sharpe.
    """
    if len(daily_pnls) < 2:
        return None
    n = len(daily_pnls)
    mean = sum(daily_pnls) / n
    var = sum((pnl - mean) ** 2 for pnl in daily_pnls) / (n - 1)
    if var <= 0:
        return None
    return (mean / math.sqrt(var)) * math.sqrt(252)


def _compute_metrics(
    pnls: list[float],
    *,
    daily_pnls: list[float],
) -> dict[str, Any]:
    if not pnls:
        return {
            "trade_count": 0,
            "win_rate": 0.0,
            "profit_factor": None,
            "sharpe_ratio": None,
            "avg_pnl": 0.0,
            "total_pnl": 0.0,
            "max_drawdown": 0.0,
            "max_drawdown_amount": 0.0,
        }
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    profit_factor = (gross_win / gross_loss) if gross_loss > 0 else None

    cumulative: list[float] = []
    running = 0.0
    peak = 0.0
    max_drawdown_amount = 0.0
    for p in pnls:
        running += p
        cumulative.append(running)
        peak = max(peak, running)
        max_drawdown_amount = max(max_drawdown_amount, peak - running)

    # Preserve the legacy percentage field exactly for existing clients. It is
    # a percentage of the cumulative realized-PnL high-water mark, not account
    # equity; new clients should use max_drawdown_amount.
    legacy_peak = cumulative[0]
    legacy_max_drawdown = 0.0
    for cumulative_pnl in cumulative:
        legacy_peak = max(legacy_peak, cumulative_pnl)
        if legacy_peak > 0:
            legacy_max_drawdown = max(
                legacy_max_drawdown,
                (legacy_peak - cumulative_pnl) / legacy_peak,
            )

    total_pnl = sum(pnls)
    return {
        "trade_count": len(pnls),
        "win_rate": (len(wins) / len(pnls)) * 100.0,
        "profit_factor": profit_factor,
        "sharpe_ratio": _compute_sharpe(daily_pnls),
        "avg_pnl": total_pnl / len(pnls),
        "total_pnl": total_pnl,
        "max_drawdown": legacy_max_drawdown * 100.0,
        "max_drawdown_amount": max_drawdown_amount,
    }


def _trade_currency(symbol: str) -> str:
    return "HKD" if symbol.upper().endswith(".HK") else "USD"


def _realized_daily_pnls(
    trades: list[ClosedRoundTrip],
    *,
    cutoff: datetime,
    now: datetime,
) -> list[float]:
    totals: dict[date, float] = {}
    for trade in trades:
        market = "HK" if trade.symbol.upper().endswith(".HK") else "US"
        trade_day = trade_day_for(market, trade.exit_at)
        totals[trade_day] = totals.get(trade_day, 0.0) + trade.net_pnl

    observations: list[float] = []
    cursor = cutoff.date()
    end = now.date()
    while cursor <= end:
        if cursor.weekday() < 5:
            observations.append(totals.get(cursor, 0.0))
        cursor += timedelta(days=1)
    return observations


@router.get(
    "/summary",
    response_model=MetricsSummaryResponse,
    dependencies=[Depends(require_api_key())],
)
def metrics_summary(
    days: int = Query(default=30, ge=1, le=365, description="Lookback window in days"),
    db: Session = Depends(get_db),
) -> MetricsSummaryResponse:
    """Aggregate trading metrics over the last ``days`` days.

    Uses the same authoritative FIFO replay, fee schedule, exit-time window,
    and unresolved-day exclusion policy as ``/api/trades/stats``. Entries
    before the lookback remain available to match exits inside the window.
    """
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    fee_rate_us, fee_rate_hk = _active_fee_rates(db)
    replay = DailyPnlService(db).pair_round_trips_with_issues(
        fee_rate_us=fee_rate_us,
        fee_rate_hk=fee_rate_hk,
        include_excursions=False,
    )
    sample = select_statistics_sample(replay, from_dt=cutoff)
    ordered_trades = sorted(sample.trades, key=lambda trade: trade.exit_at)
    pnls = [trade.net_pnl for trade in ordered_trades]
    trades_by_currency: dict[str, list[ClosedRoundTrip]] = {}
    for trade in ordered_trades:
        trades_by_currency.setdefault(
            _trade_currency(trade.symbol),
            [],
        ).append(trade)

    metrics = _compute_metrics(
        pnls,
        daily_pnls=_realized_daily_pnls(
            ordered_trades,
            cutoff=cutoff,
            now=now,
        ),
    )
    metrics["window_days"] = days
    currencies = sorted(trades_by_currency)
    metrics["currency"] = (
        currencies[0]
        if len(currencies) == 1
        else "MIXED"
        if currencies
        else None
    )
    metrics["totals_comparable"] = len(currencies) <= 1
    if len(currencies) > 1:
        # Counts and hit rate are dimensionless. Currency-denominated values,
        # their ratios, and their path statistics are not comparable without
        # an FX conversion, so fail closed instead of adding USD to HKD.
        metrics.update(
            {
                "profit_factor": None,
                "sharpe_ratio": None,
                "avg_pnl": None,
                "total_pnl": None,
                "max_drawdown": None,
                "max_drawdown_amount": None,
            }
        )
    metrics["by_currency"] = [
        _compute_metrics(
            [trade.net_pnl for trade in trades_by_currency[currency]],
            daily_pnls=_realized_daily_pnls(
                trades_by_currency[currency],
                cutoff=cutoff,
                now=now,
            ),
        )
        | {"currency": currency}
        for currency in currencies
    ]
    metrics["statistics_quality"] = asdict(sample.quality)
    return MetricsSummaryResponse.model_validate(metrics)
