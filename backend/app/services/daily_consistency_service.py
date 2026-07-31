"""Daily PnL consistency service.

Aggregates realized net PnL by calendar day and measures consistency:
share of green days, daily Sharpe, day-level streaks, and how much of
the total profit comes from the best few days.  Read-only.

Inspired by QuantStats' daily-consistency tear sheet metrics.
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["DailyConsistencyService"]


class DailyConsistencyService:
    """Day-level realized PnL consistency analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        daily = self._fetch(days)
        if len(daily) < 5:
            return {
                "days": days,
                "trading_days": len(daily),
                "error": "Need at least 5 trading days.",
            }

        values = [pnl for _, pnl in daily]
        n = len(values)
        total = sum(values)
        green = sum(1 for v in values if v > 0)
        red = sum(1 for v in values if v < 0)

        mean = total / n
        variance = sum((v - mean) ** 2 for v in values) / (n - 1)
        std = math.sqrt(variance)
        daily_sharpe = (mean / std) * math.sqrt(252) if std > 0 else None

        # day-level streaks (consecutive green / red days)
        longest_green = longest_red = 0
        cur_green = cur_red = 0
        for v in values:
            if v > 0:
                cur_green += 1
                cur_red = 0
            elif v < 0:
                cur_red += 1
                cur_green = 0
            else:
                cur_green = cur_red = 0
            longest_green = max(longest_green, cur_green)
            longest_red = max(longest_red, cur_red)

        # current streak (walk back from the most recent day)
        current_streak = 0
        if values:
            sign = 1 if values[-1] > 0 else -1 if values[-1] < 0 else 0
            for v in reversed(values):
                if sign > 0 and v > 0:
                    current_streak += 1
                elif sign < 0 and v < 0:
                    current_streak -= 1
                else:
                    break

        gross_profit = sum(v for v in values if v > 0)
        top5 = sum(sorted((v for v in values if v > 0), reverse=True)[:5])
        top5_share = top5 / gross_profit if gross_profit > 0 else None

        best = max(daily, key=lambda dp: dp[1])
        worst = min(daily, key=lambda dp: dp[1])

        series = [{"date": d, "pnl": round(p, 2)} for d, p in daily]

        return {
            "days": days,
            "trading_days": n,
            "total_pnl": round(total, 2),
            "green_days": green,
            "red_days": red,
            "green_day_pct": round(green / n, 4),
            "avg_daily_pnl": round(mean, 2),
            "daily_std": round(std, 2),
            "daily_sharpe": round(daily_sharpe, 2) if daily_sharpe is not None else None,
            "longest_green_streak": longest_green,
            "longest_red_streak": longest_red,
            "current_streak": current_streak,
            "top5_day_profit_share": round(top5_share, 4) if top5_share is not None else None,
            "best_day": {"date": best[0], "pnl": round(best[1], 2)},
            "worst_day": {"date": worst[0], "pnl": round(worst[1], 2)},
            "daily": series,
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.filled_at, OrderRecord.net_pnl)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        by_day: dict[str, float] = defaultdict(float)
        for filled_at, pnl in self._db.execute(stmt).all():
            if filled_at is not None and pnl is not None:
                by_day[filled_at.date().isoformat()] += float(pnl)
        return sorted(by_day.items())
