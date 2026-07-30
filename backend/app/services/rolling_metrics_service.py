"""Rolling performance metrics service.

Computes rolling Sharpe ratio, win-rate, and average PnL over a sliding
window of trades to surface regime changes and strategy decay.  Read-only.

Inspired by VectorBT's rolling analytics and QuantStats' rolling tearsheet.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RollingMetricsService"]


class RollingMetricsService:
    """Sliding-window performance metrics over trade sequence."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        window: int = 20,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < window:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "window": window,
                "sample_size": len(pnls),
                "error": f"Need at least {window} trades for window={window}.",
            }

        points: list[dict[str, Any]] = []
        for i in range(window - 1, len(pnls)):
            w = pnls[i - window + 1 : i + 1]
            total = sum(w)
            wins = sum(1 for p in w if p > 0)
            mean = total / window
            var = sum((p - mean) ** 2 for p in w) / window
            std = math.sqrt(var) if var > 0 else 0.0
            sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0.0
            points.append(
                {
                    "index": i,
                    "total_pnl": round(total, 2),
                    "avg_pnl": round(mean, 2),
                    "win_rate": round(wins / window, 4),
                    "sharpe": round(sharpe, 4),
                }
            )

        # summary of the rolling series
        sharpes = [p["sharpe"] for p in points]
        win_rates = [p["win_rate"] for p in points]
        latest = points[-1] if points else None

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "window": window,
            "sample_size": len(pnls),
            "points": points,
            "summary": {
                "sharpe_mean": round(sum(sharpes) / len(sharpes), 4),
                "sharpe_min": round(min(sharpes), 4),
                "sharpe_max": round(max(sharpes), 4),
                "win_rate_mean": round(sum(win_rates) / len(win_rates), 4),
                "latest_sharpe": latest["sharpe"] if latest else 0,
                "latest_win_rate": latest["win_rate"] if latest else 0,
                "trend": _trend(sharpes),
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


def _trend(values: list[float]) -> str:
    if len(values) < 4:
        return "insufficient"
    first_half = sum(values[: len(values) // 2]) / (len(values) // 2)
    second_half = sum(values[len(values) // 2 :]) / (len(values) - len(values) // 2)
    diff = second_half - first_half
    if diff > 0.1:
        return "improving"
    if diff < -0.1:
        return "decaying"
    return "stable"
