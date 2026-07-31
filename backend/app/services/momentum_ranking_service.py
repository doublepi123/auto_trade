"""Symbol momentum ranking service.

Ranks traded symbols by recent PnL momentum (rolling cumulative PnL slope)
to identify which symbols are trending favorably or unfavorably.
Read-only.

Inspired by QuantConnect's momentum ranking and Lean's cross-sectional
signal generation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["MomentumRankingService"]


class MomentumRankingService:
    """Cross-sectional PnL momentum ranking across symbols."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def rank(
        self, lookback_days: int = 90, min_trades: int = 3
    ) -> dict[str, Any]:
        rows = self._fetch(lookback_days)
        if len(rows) < 5:
            return {
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        # group by symbol, preserving time order
        by_symbol: dict[str, list[float]] = defaultdict(list)
        for sym, pnl in rows:
            by_symbol[sym].append(pnl)

        rankings: list[dict[str, Any]] = []
        for sym, pnls in by_symbol.items():
            if len(pnls) < min_trades:
                continue
            total = sum(pnls)
            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0)

            # momentum: slope of cumulative PnL (linear regression)
            cum = 0.0
            cum_series: list[float] = []
            for p in pnls:
                cum += p
                cum_series.append(cum)

            slope = _slope(cum_series)

            # recent vs older performance
            half = n // 2
            recent = sum(pnls[half:])
            older = sum(pnls[:half])
            acceleration = recent - older

            rankings.append(
                {
                    "symbol": sym,
                    "trade_count": n,
                    "total_pnl": round(total, 2),
                    "win_rate": round(wins / n, 4),
                    "momentum_slope": round(slope, 4),
                    "recent_pnl": round(recent, 2),
                    "older_pnl": round(older, 2),
                    "acceleration": round(acceleration, 2),
                }
            )

        rankings.sort(key=lambda x: x["momentum_slope"], reverse=True)
        for i, r in enumerate(rankings):
            r["rank"] = i + 1

        top = rankings[0] if rankings else None
        bottom = rankings[-1] if rankings else None

        return {
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "qualifying_symbols": len(rankings),
            "rankings": rankings,
            "top_momentum": top,
            "bottom_momentum": bottom,
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.symbol, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.execute(stmt).all()
        return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0
