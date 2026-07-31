"""Trade frequency analysis service.

Detects overtrading patterns by measuring trades-per-day distribution,
inter-trade intervals, and clustering.  Read-only.

Inspired by Freqtrade's trade-frequency stats and Edgewonk's discipline
metrics.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    trade_local_day,
)

__all__ = ["TradeFrequencyService"]


class TradeFrequencyService:
    """Trade frequency and overtrading detection."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 90
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        trades = sample.trades
        timestamps = [trade.exit_at for trade in trades]
        if len(timestamps) < 5:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(timestamps),
                "error": "Need at least 5 trades.",
            })

        # trades per day
        day_counts: Counter[str] = Counter()
        for trade in trades:
            day_counts[trade_local_day(trade.symbol, trade.exit_at).isoformat()] += 1

        counts = list(day_counts.values())
        active_days = len(counts)
        total_trades = len(timestamps)
        avg_per_day = total_trades / max(active_days, 1)
        max_day = max(counts)
        max_day_date = max(day_counts, key=day_counts.get)  # type: ignore[arg-type]

        # Inter-trade intervals only compare closes for the same symbol and
        # market-local trade day. Overnight, cross-market, and cross-symbol
        # gaps are not evidence for or against rapid-fire trading.
        timestamps_by_symbol_day: dict[tuple[str, str], list[datetime]] = defaultdict(list)
        for trade in trades:
            local_day = trade_local_day(trade.symbol, trade.exit_at).isoformat()
            timestamps_by_symbol_day[(trade.symbol, local_day)].append(trade.exit_at)

        intervals: list[float] = []
        for group in timestamps_by_symbol_day.values():
            ordered = sorted(group)
            intervals.extend(
                (ordered[index] - ordered[index - 1]).total_seconds()
                for index in range(1, len(ordered))
            )

        avg_interval = sum(intervals) / len(intervals) if intervals else 0
        min_interval = min(intervals) if intervals else 0

        # rapid-fire detection (< 60s between trades)
        rapid = sum(1 for iv in intervals if iv < 60)
        rapid_pct = rapid / len(intervals) if intervals else 0

        # distribution of daily counts
        freq_dist: Counter[int] = Counter(counts)
        daily_distribution = [
            {"trades_per_day": k, "day_count": v}
            for k, v in sorted(freq_dist.items())
        ]

        overtrading = avg_per_day > 10 or rapid_pct > 0.3

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "total_trades": total_trades,
            "active_days": active_days,
            "avg_trades_per_day": round(avg_per_day, 2),
            "max_trades_in_day": max_day,
            "max_day_date": max_day_date,
            "avg_interval_seconds": round(avg_interval, 1),
            "min_interval_seconds": round(min_interval, 1),
            "interval_pair_count": len(intervals),
            "rapid_fire_count": rapid,
            "rapid_fire_pct": round(rapid_pct, 4),
            "daily_distribution": daily_distribution,
            "overtrading_flag": overtrading,
            "assessment": _assess(avg_per_day, rapid_pct, overtrading),
        })


def _assess(avg: float, rapid_pct: float, flag: bool) -> str:
    if flag:
        return "Overtrading signals detected — high frequency or rapid-fire clustering. Consider cooldown rules."
    if avg > 5:
        return "Moderate-to-high frequency. Monitor for discipline drift."
    return "Trade frequency is within normal bounds."
