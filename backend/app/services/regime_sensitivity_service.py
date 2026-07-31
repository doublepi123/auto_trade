"""Regime sensitivity analysis service.

Splits trades into high/low volatility regimes (based on rolling PnL
standard deviation) and compares performance across regimes.  Read-only.

Inspired by VectorBT's regime analytics and QuantStats' conditional
performance tearsheet.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RegimeSensitivityService"]


class RegimeSensitivityService:
    """Performance comparison across volatility regimes."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        window: int = 20,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < window * 2:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": f"Need at least {window * 2} trades.",
            }

        # compute rolling volatility
        vols: list[float] = []
        for i in range(len(pnls)):
            if i < window - 1:
                vols.append(0.0)
                continue
            w = pnls[i - window + 1 : i + 1]
            mean = sum(w) / window
            var = sum((p - mean) ** 2 for p in w) / window
            vols.append(math.sqrt(var))

        # split into regimes by median volatility
        active_vols = [v for v in vols if v > 0]
        if not active_vols:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "No volatility variation detected.",
            }

        median_vol = sorted(active_vols)[len(active_vols) // 2]

        low_vol_pnls: list[float] = []
        high_vol_pnls: list[float] = []
        for i, (pnl, vol) in enumerate(zip(pnls, vols)):
            if i < window - 1:
                continue
            if vol <= median_vol:
                low_vol_pnls.append(pnl)
            else:
                high_vol_pnls.append(pnl)

        def _regime_stats(pnls_list: list[float], label: str) -> dict[str, Any]:
            if not pnls_list:
                return {"regime": label, "trade_count": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0, "sharpe": 0}
            n = len(pnls_list)
            wins = sum(1 for p in pnls_list if p > 0)
            total = sum(pnls_list)
            mean = total / n
            var = sum((p - mean) ** 2 for p in pnls_list) / n
            std = math.sqrt(var) if var > 0 else 0
            sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
            return {
                "regime": label,
                "trade_count": n,
                "win_rate": round(wins / n, 4),
                "avg_pnl": round(mean, 2),
                "total_pnl": round(total, 2),
                "sharpe": round(sharpe, 4),
            }

        low_stats = _regime_stats(low_vol_pnls, "low_volatility")
        high_stats = _regime_stats(high_vol_pnls, "high_volatility")

        # regime sensitivity: difference in Sharpe
        sensitivity = high_stats["sharpe"] - low_stats["sharpe"]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "window": window,
            "median_volatility": round(median_vol, 4),
            "regimes": [low_stats, high_stats],
            "sensitivity": round(sensitivity, 4),
            "interpretation": _interpret(sensitivity, low_stats, high_stats),
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


def _interpret(sens: float, low: dict, high: dict) -> str:
    if sens > 0.5:
        return "Strategy performs better in high-volatility regimes — thrives on turbulence."
    if sens < -0.5:
        return "Strategy performs better in low-volatility regimes — sensitive to turbulence. Consider reducing size in volatile periods."
    return "Strategy performance is relatively stable across volatility regimes."
