"""First-trade-of-day effect service.

Splits each trading day's closed trades into the first closing trade and
all subsequent ones, comparing win rate and PnL, and measures how often
the first close's outcome matches the subsequent closes' combined sign.
Read-only.

Inspired by QuantConnect/Lean's session-open research notebooks.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
    trade_local_day,
)

__all__ = ["FirstTradeService"]

_MIN_TONE_SAMPLE_DAYS = 5


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
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            payload={"days": days, "sample_size": len(sample.trades)},
        )
        if mixed_error is not None:
            return mixed_error
        rows = [
            (
                trade_local_day(trade.symbol, trade.exit_at).isoformat(),
                trade.net_pnl,
            )
            for trade in sample.trades
        ]
        if len(rows) < 5:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(rows),
                    "error": "Need at least 5 closed trades.",
                },
            )

        by_day: dict[str, list[float]] = defaultdict(list)
        for day, pnl in rows:
            by_day[day].append(pnl)

        first = _Bucket()
        rest = _Bucket()
        tone_match = 0
        tone_sample_days = 0
        green_days = 0

        for pnls in by_day.values():
            day_total = sum(pnls)
            if day_total > 0:
                green_days += 1
            first_pnl = pnls[0]
            first.add(first_pnl)
            rest_pnls = pnls[1:]
            for p in rest_pnls:
                rest.add(p)
            rest_total = sum(rest_pnls)
            if rest_pnls and first_pnl != 0 and rest_total != 0:
                tone_sample_days += 1
                if (first_pnl > 0) == (rest_total > 0):
                    tone_match += 1

        n_days = len(by_day)
        multi_trade_days = sum(1 for pnls in by_day.values() if len(pnls) > 1)
        tone_sample_sufficient = tone_sample_days >= _MIN_TONE_SAMPLE_DAYS

        return analytics_response(
            sample,
            {
                "days": days,
                "sample_size": len(rows),
                "trading_days": n_days,
                "green_day_pct": round(green_days / n_days, 4) if n_days else None,
                "multi_trade_days": multi_trade_days,
                "first_trade": first.as_dict(),
                "rest_of_day": rest.as_dict(),
                "tone_match_pct": (
                    round(tone_match / tone_sample_days, 4)
                    if tone_sample_sufficient
                    else None
                ),
                "tone_match_count": tone_match,
                "tone_sample_days": tone_sample_days,
                "tone_min_sample_days": _MIN_TONE_SAMPLE_DAYS,
                "tone_sample_sufficient": tone_sample_sufficient,
            },
        )
