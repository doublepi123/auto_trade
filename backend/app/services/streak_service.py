"""Trade streak analysis service.

Computes win/loss streak distributions, current streaks, and the
probability of observing streaks of a given length under the empirical
win-rate.  Read-only.

Inspired by QuantStats' streak analysis and Freqtrade's trade statistics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["StreakService"]


class StreakService:
    """Win/loss streak distribution and probability analysis."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 3:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 3 closed trades.",
            }

        outcomes = [p > 0 for p in pnls]
        win_rate = sum(outcomes) / len(outcomes)

        # extract streaks
        win_streaks: list[int] = []
        loss_streaks: list[int] = []
        current_streak_type: bool = outcomes[0]
        current_len = 1
        for o in outcomes[1:]:
            if o == current_streak_type:
                current_len += 1
            else:
                (win_streaks if current_streak_type else loss_streaks).append(current_len)
                current_streak_type = o
                current_len = 1
        (win_streaks if current_streak_type else loss_streaks).append(current_len)

        # current active streak (from the end)
        active_type = outcomes[-1]
        active_len = 0
        for o in reversed(outcomes):
            if o == active_type:
                active_len += 1
            else:
                break

        max_win = max(win_streaks) if win_streaks else 0
        max_loss = max(loss_streaks) if loss_streaks else 0

        # probability of a streak of length k under empirical win-rate
        def streak_prob(k: int, p: float) -> float:
            return round(p**k, 6)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "win_rate": round(win_rate, 4),
            "current_streak": {
                "type": "win" if active_type else "loss",
                "length": active_len,
            },
            "win_streaks": {
                "count": len(win_streaks),
                "max": max_win,
                "avg": round(sum(win_streaks) / len(win_streaks), 2) if win_streaks else 0,
                "distribution": _histogram(win_streaks),
            },
            "loss_streaks": {
                "count": len(loss_streaks),
                "max": max_loss,
                "avg": round(sum(loss_streaks) / len(loss_streaks), 2) if loss_streaks else 0,
                "distribution": _histogram(loss_streaks),
            },
            "probability": {
                "win_streak_3": streak_prob(3, win_rate),
                "win_streak_5": streak_prob(5, win_rate),
                "loss_streak_3": streak_prob(3, 1 - win_rate),
                "loss_streak_5": streak_prob(5, 1 - win_rate),
            },
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]


def _histogram(streaks: list[int]) -> list[dict[str, Any]]:
    from collections import Counter

    counts = Counter(streaks)
    return [
        {"length": k, "count": v}
        for k, v in sorted(counts.items())
    ]
