"""Trade frequency analysis service.

Detects overtrading patterns by measuring trades-per-day distribution,
inter-trade intervals, and clustering.  Read-only.

Inspired by Freqtrade's trade-frequency stats and Edgewonk's discipline
metrics.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["TradeFrequencyService"]


class TradeFrequencyService:
    """Trade frequency and overtrading detection."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 90
    ) -> dict[str, Any]:
        timestamps = self._fetch(symbol, lookback_days)
        if len(timestamps) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(timestamps),
                "error": "Need at least 5 trades.",
            }

        # trades per day
        day_counts: Counter[str] = Counter()
        for ts in timestamps:
            day_counts[ts.strftime("%Y-%m-%d")] += 1

        counts = list(day_counts.values())
        active_days = len(counts)
        total_trades = len(timestamps)
        avg_per_day = total_trades / max(active_days, 1)
        max_day = max(counts)
        max_day_date = max(day_counts, key=day_counts.get)  # type: ignore[arg-type]

        # inter-trade intervals (seconds)
        intervals: list[float] = []
        for i in range(1, len(timestamps)):
            delta = (timestamps[i] - timestamps[i - 1]).total_seconds()
            if delta >= 0:
                intervals.append(delta)

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

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "total_trades": total_trades,
            "active_days": active_days,
            "avg_trades_per_day": round(avg_per_day, 2),
            "max_trades_in_day": max_day,
            "max_day_date": max_day_date,
            "avg_interval_seconds": round(avg_interval, 1),
            "min_interval_seconds": round(min_interval, 1),
            "rapid_fire_count": rapid,
            "rapid_fire_pct": round(rapid_pct, 4),
            "daily_distribution": daily_distribution,
            "overtrading_flag": overtrading,
            "assessment": _assess(avg_per_day, rapid_pct, overtrading),
        }

    def _fetch(self, symbol: str | None, days: int) -> list[datetime]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.filled_at).where(
            OrderRecord.filled_at.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.scalars(stmt).all()
        return [r for r in rows if r is not None]


def _assess(avg: float, rapid_pct: float, flag: bool) -> str:
    if flag:
        return "Overtrading signals detected — high frequency or rapid-fire clustering. Consider cooldown rules."
    if avg > 5:
        return "Moderate-to-high frequency. Monitor for discipline drift."
    return "Trade frequency is within normal bounds."
