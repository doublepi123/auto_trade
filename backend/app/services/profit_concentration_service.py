"""Profit concentration (Pareto) analysis service.

Measures how concentrated profits are in a small fraction of winning
trades: top-N% trade share of gross profit, a Lorenz-style cumulative
curve, and a Gini coefficient over winning-trade PnL.  Read-only.

Inspired by QuantStats' Pareto-style profit concentration reports.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ProfitConcentrationService"]

_LEVELS = (0.01, 0.05, 0.10, 0.20, 0.50, 1.0)


class ProfitConcentrationService:
    """Trade-level profit concentration analytics."""

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

        wins = sorted((p for p in rows if p > 0), reverse=True)
        losses = [p for p in rows if p < 0]
        total_win = sum(wins)
        total_loss = sum(losses)

        if not wins or total_win <= 0:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "No winning trades in window.",
            }

        n_wins = len(wins)
        pareto: list[dict[str, float]] = []
        for level in _LEVELS:
            k = max(1, round(n_wins * level))
            share = sum(wins[:k]) / total_win
            pareto.append(
                {
                    "top_pct_trades": level,
                    "trade_count": k,
                    "profit_share": round(share, 4),
                }
            )

        gini = _gini(wins)

        top_trade = wins[0]
        top5 = sum(wins[: min(5, n_wins)])

        return {
            "days": days,
            "sample_size": len(rows),
            "winning_trades": n_wins,
            "losing_trades": len(losses),
            "gross_profit": round(total_win, 2),
            "gross_loss": round(total_loss, 2),
            "top_trade_pnl": round(top_trade, 2),
            "top_trade_share": round(top_trade / total_win, 4),
            "top5_share": round(top5 / total_win, 4),
            "gini_winners": round(gini, 4),
            "pareto_curve": pareto,
        }

    def _fetch(self, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.net_pnl)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return [float(r[0]) for r in self._db.execute(stmt).all() if r[0] is not None]


def _gini(sorted_desc: list[float]) -> float:
    """Gini coefficient of a non-negative series (sorted descending)."""
    n = len(sorted_desc)
    total = sum(sorted_desc)
    if n == 0 or total <= 0:
        return 0.0
    # G = (2 * sum(i * x_i) / (n * sum(x))) - (n + 1) / n, x sorted ascending
    asc = list(reversed(sorted_desc))
    weighted = sum((i + 1) * x for i, x in enumerate(asc))
    return (2.0 * weighted) / (n * total) - (n + 1.0) / n
