"""Weekly/monthly return calendar service.

Aggregates realized PnL into ISO-week and calendar-month buckets to
produce a return calendar heatmap.  Read-only.

Inspired by QuantStats' monthly returns tearsheet and VectorBT's
periodic return aggregation.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    market_local_datetime,
    mixed_currency_error,
)

__all__ = ["ReturnCalendarService"]


class ReturnCalendarService:
    """ISO-week and calendar-month PnL aggregation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            symbol=symbol,
            lookback_days=lookback_days,
        )
        if mixed_error is not None:
            return mixed_error
        rows = [
            (market_local_datetime(trade.symbol, trade.exit_at), trade.net_pnl)
            for trade in sample.trades
        ]
        if len(rows) < 5:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            })

        weekly: dict[str, float] = defaultdict(float)
        monthly: dict[str, float] = defaultdict(float)
        weekly_counts: dict[str, int] = defaultdict(int)
        monthly_counts: dict[str, int] = defaultdict(int)

        for ts, pnl in rows:
            iso = ts.isocalendar()
            wk = f"{iso[0]}-W{iso[1]:02d}"
            mo = ts.strftime("%Y-%m")
            weekly[wk] += pnl
            monthly[mo] += pnl
            weekly_counts[wk] += 1
            monthly_counts[mo] += 1

        weekly_list = [
            {
                "period": k,
                "pnl": round(v, 2),
                "trade_count": weekly_counts[k],
            }
            for k, v in sorted(weekly.items())
        ]
        monthly_list = [
            {
                "period": k,
                "pnl": round(v, 2),
                "trade_count": monthly_counts[k],
            }
            for k, v in sorted(monthly.items())
        ]

        pos_weeks = sum(1 for w in weekly_list if w["pnl"] > 0)
        pos_months = sum(1 for m in monthly_list if m["pnl"] > 0)

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "weekly": weekly_list,
            "monthly": monthly_list,
            "summary": {
                "total_weeks": len(weekly_list),
                "positive_weeks": pos_weeks,
                "weekly_win_rate": round(pos_weeks / len(weekly_list), 4) if weekly_list else 0,
                "total_months": len(monthly_list),
                "positive_months": pos_months,
                "monthly_win_rate": round(pos_months / len(monthly_list), 4) if monthly_list else 0,
                "best_week": max(weekly_list, key=lambda x: x["pnl"]) if weekly_list else None,
                "worst_week": min(weekly_list, key=lambda x: x["pnl"]) if weekly_list else None,
                "best_month": max(monthly_list, key=lambda x: x["pnl"]) if monthly_list else None,
                "worst_month": min(monthly_list, key=lambda x: x["pnl"]) if monthly_list else None,
            },
        })
