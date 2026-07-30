"""Time-of-day performance analysis service.

Breaks down realized PnL by hour-of-day and day-of-week to surface
temporal edges or anti-edges.  Read-only.

Inspired by Freqtrade's trade-time analysis and QuantStats' periodic
returns tearsheet.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["TimePerformanceService"]

_DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class TimePerformanceService:
    """PnL breakdown by hour-of-day and day-of-week."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        by_hour: dict[int, list[float]] = defaultdict(list)
        by_dow: dict[int, list[float]] = defaultdict(list)

        for ts, pnl in rows:
            by_hour[ts.hour].append(pnl)
            by_dow[ts.weekday()].append(pnl)

        hour_stats = [
            _bucket_stats(h, pnls) for h, pnls in sorted(by_hour.items())
        ]
        dow_stats = [
            {**_bucket_stats(d, pnls), "day_name": _DAY_NAMES[d]}
            for d, pnls in sorted(by_dow.items())
        ]

        best_hour = max(hour_stats, key=lambda x: x["total_pnl"]) if hour_stats else None
        worst_hour = min(hour_stats, key=lambda x: x["total_pnl"]) if hour_stats else None
        best_dow = max(dow_stats, key=lambda x: x["total_pnl"]) if dow_stats else None
        worst_dow = min(dow_stats, key=lambda x: x["total_pnl"]) if dow_stats else None

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "by_hour": hour_stats,
            "by_day_of_week": dow_stats,
            "highlights": {
                "best_hour": best_hour,
                "worst_hour": worst_hour,
                "best_day": best_dow,
                "worst_day": worst_dow,
            },
        }

    def _fetch(
        self, symbol: str | None, days: int
    ) -> list[tuple[datetime, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.filled_at, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.execute(stmt).all()
        return [
            (r[0], float(r[1]))
            for r in rows
            if r[0] is not None and r[1] is not None
        ]


def _bucket_stats(key: int, pnls: list[float]) -> dict[str, Any]:
    total = sum(pnls)
    wins = sum(1 for p in pnls if p > 0)
    n = len(pnls)
    return {
        "bucket": key,
        "trade_count": n,
        "total_pnl": round(total, 2),
        "avg_pnl": round(total / n, 2) if n else 0.0,
        "win_rate": round(wins / n, 4) if n else 0.0,
    }
