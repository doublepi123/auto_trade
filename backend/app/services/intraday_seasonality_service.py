"""Intraday seasonality analysis service.

Computes average PnL by 30-minute intraday bucket to reveal time-of-day
edges within the trading session.  Read-only.

Inspired by Freqtrade's intraday seasonality module and QuantStats'
periodic return analysis.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["IntradaySeasonalityService"]

# 30-minute buckets covering US RTH (09:30 - 16:00 ET)
_BUCKETS = [
    (9 * 60 + 30, 10 * 60, "09:30-10:00"),
    (10 * 60, 10 * 60 + 30, "10:00-10:30"),
    (10 * 60 + 30, 11 * 60, "10:30-11:00"),
    (11 * 60, 11 * 60 + 30, "11:00-11:30"),
    (11 * 60 + 30, 12 * 60, "11:30-12:00"),
    (12 * 60, 12 * 60 + 30, "12:00-12:30"),
    (12 * 60 + 30, 13 * 60, "12:30-13:00"),
    (13 * 60, 13 * 60 + 30, "13:00-13:30"),
    (13 * 60 + 30, 14 * 60, "13:30-14:00"),
    (14 * 60, 14 * 60 + 30, "14:00-14:30"),
    (14 * 60 + 30, 15 * 60, "14:30-15:00"),
    (15 * 60, 15 * 60 + 30, "15:00-15:30"),
    (15 * 60 + 30, 16 * 60, "15:30-16:00"),
]


class IntradaySeasonalityService:
    """Average PnL by 30-minute intraday bucket."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 10:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 10 closed trades.",
            }

        by_bucket: dict[str, list[float]] = defaultdict(list)
        unmatched: list[float] = []

        for ts, pnl in rows:
            minutes = ts.hour * 60 + ts.minute
            matched = False
            for lo, hi, label in _BUCKETS:
                if lo <= minutes < hi:
                    by_bucket[label].append(pnl)
                    matched = True
                    break
            if not matched:
                unmatched.append(pnl)

        stats: list[dict[str, Any]] = []
        for _, _, label in _BUCKETS:
            pnls = by_bucket.get(label, [])
            if not pnls:
                stats.append({"bucket": label, "trade_count": 0, "avg_pnl": 0, "total_pnl": 0, "win_rate": 0})
                continue
            total = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            stats.append(
                {
                    "bucket": label,
                    "trade_count": len(pnls),
                    "avg_pnl": round(total / len(pnls), 2),
                    "total_pnl": round(total, 2),
                    "win_rate": round(wins / len(pnls), 4),
                }
            )

        active = [s for s in stats if s["trade_count"] > 0]
        best = max(active, key=lambda x: x["avg_pnl"]) if active else None
        worst = min(active, key=lambda x: x["avg_pnl"]) if active else None

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "buckets": stats,
            "unmatched_count": len(unmatched),
            "best_bucket": best,
            "worst_bucket": worst,
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
