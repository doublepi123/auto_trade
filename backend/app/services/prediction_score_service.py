"""Trade outcome prediction score service.

Computes a simple feature-based score for each trade using historical
win-rate conditional on observable features (day-of-week, hour, recent
streak).  Read-only.

Inspired by Freqtrade's hyperopt feature importance and Edgewonk's
trade grading system.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["PredictionScoreService"]


class PredictionScoreService:
    """Conditional win-rate scoring by observable features."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 20:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 20 closed trades.",
            }

        # build conditional win-rates
        by_dow: dict[int, list[bool]] = defaultdict(list)
        by_hour: dict[int, list[bool]] = defaultdict(list)
        by_streak: dict[str, list[bool]] = defaultdict(list)

        streak = 0
        for ts, pnl in rows:
            win = pnl > 0
            by_dow[ts.weekday()].append(win)
            by_hour[ts.hour].append(win)
            streak_label = "after_win" if streak > 0 else "after_loss" if streak < 0 else "neutral"
            by_streak[streak_label].append(win)
            streak = streak + 1 if win else -(abs(streak) + 1) if not win else 0

        def _wr(outcomes: list[bool]) -> float:
            return sum(outcomes) / len(outcomes) if outcomes else 0.5

        dow_wr = {d: round(_wr(v), 4) for d, v in sorted(by_dow.items())}
        hour_wr = {h: round(_wr(v), 4) for h, v in sorted(by_hour.items())}
        streak_wr = {k: round(_wr(v), 4) for k, v in by_streak.items()}

        # overall baseline
        all_wins = [pnl > 0 for _, pnl in rows]
        baseline_wr = sum(all_wins) / len(all_wins)

        # best/worst conditional edges
        all_features: list[tuple[str, float, int]] = []
        for d, wr in dow_wr.items():
            all_features.append((f"dow_{d}", wr, len(by_dow[d])))
        for h, wr in hour_wr.items():
            if len(by_hour[h]) >= 3:
                all_features.append((f"hour_{h}", wr, len(by_hour[h])))

        all_features.sort(key=lambda x: x[1], reverse=True)
        top_edges = all_features[:5]
        bottom_edges = all_features[-5:]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "baseline_win_rate": round(baseline_wr, 4),
            "dow_win_rates": dow_wr,
            "hour_win_rates": hour_wr,
            "streak_win_rates": streak_wr,
            "top_edges": [{"feature": f, "win_rate": wr, "count": c} for f, wr, c in top_edges],
            "bottom_edges": [{"feature": f, "win_rate": wr, "count": c} for f, wr, c in bottom_edges],
            "edge_spread": round(top_edges[0][1] - bottom_edges[-1][1], 4) if top_edges and bottom_edges else 0,
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
