"""Trade size impact analysis service.

Measures whether larger position sizes produce proportionally better or
worse risk-adjusted returns, detecting capacity constraints or sizing
misalignment.  Read-only.

Inspired by Freqtrade's stake-amount analysis and QuantConnect's capacity
estimation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["SizeImpactService"]


class SizeImpactService:
    """PnL efficiency by position size quartile."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 8:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 8 closed trades with quantity data.",
            }

        # sort by quantity to form quartiles
        rows.sort(key=lambda r: r[0])
        n = len(rows)
        q_size = max(1, n // 4)
        quartiles: list[list[tuple[float, float]]] = [
            rows[i * q_size : (i + 1) * q_size] for i in range(3)
        ]
        quartiles.append(rows[3 * q_size :])

        labels = ["Q1 (smallest)", "Q2", "Q3", "Q4 (largest)"]
        stats: list[dict[str, Any]] = []
        for label, group in zip(labels, quartiles):
            if not group:
                continue
            qtys = [q for q, _ in group]
            pnls = [p for _, p in group]
            total_pnl = sum(pnls)
            avg_qty = sum(qtys) / len(qtys)
            wins = sum(1 for p in pnls if p > 0)
            # pnl per unit of quantity
            pnl_per_unit = total_pnl / sum(qtys) if sum(qtys) > 0 else 0
            stats.append(
                {
                    "quartile": label,
                    "trade_count": len(group),
                    "avg_quantity": round(avg_qty, 1),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(total_pnl / len(group), 2),
                    "win_rate": round(wins / len(group), 4),
                    "pnl_per_unit": round(pnl_per_unit, 4),
                }
            )

        # detect size efficiency trend
        if len(stats) >= 2:
            first_eff = stats[0]["pnl_per_unit"]
            last_eff = stats[-1]["pnl_per_unit"]
            if last_eff > first_eff * 1.2:
                trend = "increasing-returns"
            elif last_eff < first_eff * 0.8:
                trend = "diminishing-returns"
            else:
                trend = "stable"
        else:
            trend = "insufficient"

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "quartiles": stats,
            "size_efficiency_trend": trend,
            "assessment": _assess(trend),
        }

    def _fetch(
        self, symbol: str | None, days: int
    ) -> list[tuple[float, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.quantity, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.quantity.is_not(None),
            OrderRecord.quantity > 0,
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        rows = self._db.execute(stmt).all()
        return [
            (float(r[0]), float(r[1]))
            for r in rows
            if r[0] is not None and r[1] is not None
        ]


def _assess(trend: str) -> str:
    if trend == "increasing-returns":
        return "Larger sizes produce better per-unit returns — capacity not yet constrained."
    if trend == "diminishing-returns":
        return "Larger sizes show diminishing per-unit returns — approaching capacity or slippage limits."
    if trend == "stable":
        return "Per-unit returns are stable across size quartiles — sizing is well-calibrated."
    return "Insufficient data to assess size efficiency."
