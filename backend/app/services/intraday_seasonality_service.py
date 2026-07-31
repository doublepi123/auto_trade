"""Intraday seasonality analysis service.

Computes average PnL by 30-minute intraday bucket to reveal time-of-day
edges within the trading session.  Read-only.

Inspired by Freqtrade's intraday seasonality module and QuantStats'
periodic return analysis.
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
            (
                market_local_datetime(trade.symbol, trade.exit_at),
                trade.net_pnl,
            )
            for trade in sample.trades
        ]
        if len(rows) < 10:
            return analytics_response(
                sample,
                {
                    "symbol": symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(rows),
                    "error": "Need at least 10 closed trades.",
                },
            )

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

        return analytics_response(
            sample,
            {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "buckets": stats,
                "unmatched_count": len(unmatched),
                "best_bucket": best,
                "worst_bucket": worst,
            },
        )
