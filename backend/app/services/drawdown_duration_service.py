"""Drawdown duration distribution service.

Measures how long drawdown episodes last (in number of trades) and builds
a duration distribution to assess recovery speed.  Read-only.

Inspired by QuantStats' underwater duration analysis and VectorBT's
drawdown analytics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["DrawdownDurationService"]


class DrawdownDurationService:
    """Drawdown episode duration distribution."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 10:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 10 closed trades.",
            }

        # detect drawdown durations
        cum = 0.0
        peak = 0.0
        durations: list[int] = []
        current_dd_len = 0

        for p in pnls:
            cum += p
            if cum >= peak:
                if current_dd_len > 0:
                    durations.append(current_dd_len)
                    current_dd_len = 0
                peak = cum
            else:
                current_dd_len += 1

        # still underwater
        if current_dd_len > 0:
            durations.append(current_dd_len)

        if not durations:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "episodes": 0,
                "durations": [],
                "summary": {"avg": 0, "max": 0, "median": 0},
                "note": "No drawdown episodes detected.",
            }

        durations_sorted = sorted(durations)
        n = len(durations_sorted)

        # duration histogram
        from collections import Counter

        hist = Counter(durations)
        histogram = [
            {"duration": k, "count": v}
            for k, v in sorted(hist.items())
        ]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "episodes": n,
            "durations": durations_sorted[-20:],
            "histogram": histogram,
            "summary": {
                "avg": round(sum(durations) / n, 1),
                "max": max(durations),
                "median": durations_sorted[n // 2],
                "p25": durations_sorted[n // 4],
                "p75": durations_sorted[3 * n // 4],
            },
            "pct_time_underwater": round(
                sum(durations) / len(pnls) * 100, 1
            ),
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
