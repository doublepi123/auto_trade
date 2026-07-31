"""First-trade-of-day effect service.

Splits each trading day's closed trades into the first closing trade and
all subsequent ones, comparing win rate and PnL, and measures how often
the first trade's outcome matches the day's overall sign (tone setting).
Read-only.

Inspired by QuantConnect/Lean's session-open research notebooks.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["FirstTradeService"]


class _Bucket:
    __slots__ = ("n", "wins", "pnl")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.pnl = 0.0

    def add(self, pnl: float) -> None:
        self.n += 1
        if pnl > 0:
            self.wins += 1
        self.pnl += pnl

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.n,
            "win_rate": round(self.wins / self.n, 4) if self.n else None,
            "avg_pnl": round(self.pnl / self.n, 2) if self.n else None,
            "total_pnl": round(self.pnl, 2),
        }


class FirstTradeService:
    """First-closed-trade-of-day versus rest-of-day analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 5:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        by_day: dict[str, list[float]] = defaultdict(list)
        for day, pnl in rows:
            by_day[day].append(pnl)

        first = _Bucket()
        rest = _Bucket()
        tone_match = 0
        tone_days = 0
        green_days = 0

        for pnls in by_day.values():
            day_total = sum(pnls)
            if day_total > 0:
                green_days += 1
            first_pnl = pnls[0]
            first.add(first_pnl)
            for p in pnls[1:]:
                rest.add(p)
            if first_pnl != 0 and day_total != 0:
                tone_days += 1
                if (first_pnl > 0) == (day_total > 0):
                    tone_match += 1

        n_days = len(by_day)
        multi_trade_days = sum(1 for pnls in by_day.values() if len(pnls) > 1)

        return {
            "days": days,
            "sample_size": len(rows),
            "trading_days": n_days,
            "green_day_pct": round(green_days / n_days, 4) if n_days else None,
            "multi_trade_days": multi_trade_days,
            "first_trade": first.as_dict(),
            "rest_of_day": rest.as_dict(),
            "tone_match_pct": round(tone_match / tone_days, 4) if tone_days else None,
            "tone_days": tone_days,
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.filled_at, OrderRecord.net_pnl)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return [
            (r[0].date().isoformat(), float(r[1]))
            for r in self._db.execute(stmt).all()
            if r[0] is not None and r[1] is not None
        ]
