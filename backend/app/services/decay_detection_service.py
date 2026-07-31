"""Strategy decay detection service.

Splits the trade history into sequential windows and tests whether key
metrics (win-rate, expectancy, Sharpe) are deteriorating over time,
indicating edge decay.  Read-only.

Inspired by QuantStats' rolling analytics and VectorBT's strategy
degradation detection.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["DecayDetectionService"]


class DecayDetectionService:
    """Sequential-window metric degradation detection."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def detect(
        self,
        symbol: str | None = None,
        lookback_days: int = 365,
        n_windows: int = 4,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < n_windows * 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": f"Need at least {n_windows * 5} trades for {n_windows} windows.",
            }

        chunk = len(pnls) // n_windows
        windows: list[dict[str, Any]] = []
        for i in range(n_windows):
            start = i * chunk
            end = start + chunk if i < n_windows - 1 else len(pnls)
            w = pnls[start:end]
            wins = sum(1 for p in w if p > 0)
            total = sum(w)
            mean = total / len(w)
            var = sum((p - mean) ** 2 for p in w) / len(w)
            std = math.sqrt(var) if var > 0 else 0
            sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
            windows.append(
                {
                    "window": i + 1,
                    "trade_count": len(w),
                    "win_rate": round(wins / len(w), 4),
                    "total_pnl": round(total, 2),
                    "avg_pnl": round(mean, 2),
                    "sharpe": round(sharpe, 4),
                }
            )

        # linear regression slope on win_rate and sharpe across windows
        wr_slope = _slope([w["win_rate"] for w in windows])
        sharpe_slope = _slope([w["sharpe"] for w in windows])
        pnl_slope = _slope([w["avg_pnl"] for w in windows])

        # decay verdict
        decay_signals = 0
        if wr_slope < -0.02:
            decay_signals += 1
        if sharpe_slope < -0.5:
            decay_signals += 1
        if pnl_slope < -1.0:
            decay_signals += 1

        if decay_signals >= 2:
            verdict = "decaying"
        elif decay_signals == 1:
            verdict = "early-warning"
        else:
            verdict = "stable"

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "n_windows": n_windows,
            "windows": windows,
            "slopes": {
                "win_rate_per_window": round(wr_slope, 6),
                "sharpe_per_window": round(sharpe_slope, 4),
                "avg_pnl_per_window": round(pnl_slope, 4),
            },
            "decay_signals": decay_signals,
            "verdict": verdict,
            "assessment": _assess(verdict, wr_slope, sharpe_slope),
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


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0


def _assess(verdict: str, wr_slope: float, sharpe_slope: float) -> str:
    if verdict == "decaying":
        return f"Strategy edge is decaying — win-rate slope {wr_slope:.4f}/window, Sharpe slope {sharpe_slope:.3f}/window. Review parameters or retire strategy."
    if verdict == "early-warning":
        return f"Early decay signal detected (WR slope {wr_slope:.4f}, Sharpe slope {sharpe_slope:.3f}). Monitor closely."
    return "No significant decay detected — strategy metrics are stable or improving across windows."
