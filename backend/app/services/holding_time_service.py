"""Holding time analysis service.

Buckets closed trades by holding duration and computes per-bucket PnL
statistics to reveal whether short or long holds produce better outcomes.
Read-only.

Inspired by Freqtrade's trade-duration analysis and QuantStats' holding
period tearsheet.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["HoldingTimeService"]

# bucket boundaries in seconds
_BUCKETS = [
    (0, 300, "<5m"),
    (300, 900, "5-15m"),
    (900, 1800, "15-30m"),
    (1800, 3600, "30-60m"),
    (3600, 7200, "1-2h"),
    (7200, 14400, "2-4h"),
    (14400, 86400, "4-24h"),
    (86400, float("inf"), ">24h"),
]


class HoldingTimeService:
    """PnL breakdown by trade holding duration."""

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
        if any(trade.holding_seconds < 0 for trade in sample.trades):
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(sample.trades),
                "error": "Closed-trade sample contains a negative holding duration.",
            })
        rows = [
            (trade.holding_seconds, trade.net_pnl)
            for trade in sample.trades
        ]
        if len(rows) < 5:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades with duration data.",
            })

        buckets: dict[str, list[float]] = {label: [] for _, _, label in _BUCKETS}
        for duration_s, pnl in rows:
            for lo, hi, label in _BUCKETS:
                if lo <= duration_s < hi:
                    buckets[label].append(pnl)
                    break

        stats: list[dict[str, Any]] = []
        for _, _, label in _BUCKETS:
            pnls = buckets[label]
            if not pnls:
                stats.append(
                    {"bucket": label, "trade_count": 0, "total_pnl": 0, "avg_pnl": 0, "win_rate": 0}
                )
                continue
            total = sum(pnls)
            wins = sum(1 for p in pnls if p > 0)
            stats.append(
                {
                    "bucket": label,
                    "trade_count": len(pnls),
                    "total_pnl": round(total, 2),
                    "avg_pnl": round(total / len(pnls), 2),
                    "win_rate": round(wins / len(pnls), 4),
                }
            )

        durations = [d for d, _ in rows]
        best_bucket = max(
            (s for s in stats if s["trade_count"] > 0),
            key=lambda x: x["avg_pnl"],
            default=None,
        )

        ordered_durations = sorted(durations)
        midpoint = len(ordered_durations) // 2
        median = (
            ordered_durations[midpoint]
            if len(ordered_durations) % 2
            else (ordered_durations[midpoint - 1] + ordered_durations[midpoint]) / 2
        )

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "avg_holding_seconds": round(sum(durations) / len(durations), 1),
            "median_holding_seconds": round(median, 1),
            "buckets": stats,
            "best_bucket": best_bucket,
        })
