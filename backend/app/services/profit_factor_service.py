"""Profit factor decomposition service.

Breaks down the profit factor by symbol, time bucket, and trade size to
reveal which segments drive or drag overall edge.  Read-only.

Inspired by QuantStats' factor tearsheet and Edgewonk's edge decomposition.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ProfitFactorService"]


class ProfitFactorService:
    """Decompose profit factor across multiple dimensions."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        # overall
        gross_profit = sum(p for _, p in rows if p > 0)
        gross_loss = abs(sum(p for _, p in rows if p < 0))
        overall_pf = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        # by symbol
        by_symbol: dict[str, list[float]] = defaultdict(list)
        for sym, pnl in rows:
            by_symbol[sym].append(pnl)

        symbol_breakdown = [
            {"segment": sym, **_pf_stats(pnls)}
            for sym, pnls in sorted(by_symbol.items(), key=lambda x: -_pf_value(x[1]))
        ]

        # by PnL magnitude bucket
        small = [p for _, p in rows if abs(p) <= 50]
        medium = [p for _, p in rows if 50 < abs(p) <= 200]
        large = [p for _, p in rows if abs(p) > 200]

        size_breakdown = [
            {"segment": "small (≤50)", **_pf_stats(small)},
            {"segment": "medium (50-200)", **_pf_stats(medium)},
            {"segment": "large (>200)", **_pf_stats(large)},
        ]

        # by win/loss contribution
        wins = sorted([p for _, p in rows if p > 0], reverse=True)
        losses = sorted([p for _, p in rows if p < 0])
        top3_win = sum(wins[:3]) if len(wins) >= 3 else sum(wins)
        top3_loss = sum(losses[:3]) if len(losses) >= 3 else sum(losses)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "overall": {
                "profit_factor": round(overall_pf, 4) if overall_pf != float("inf") else None,
                "gross_profit": round(gross_profit, 2),
                "gross_loss": round(gross_loss, 2),
                "net_pnl": round(gross_profit - gross_loss, 2),
            },
            "by_symbol": symbol_breakdown,
            "by_size": size_breakdown,
            "concentration": {
                "top3_wins_share": round(top3_win / gross_profit, 4) if gross_profit > 0 else 0,
                "top3_losses_share": round(abs(top3_loss) / gross_loss, 4) if gross_loss > 0 else 0,
            },
        }

    def _fetch(self, symbol: str | None, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.symbol, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        rows = self._db.execute(stmt).all()
        return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _pf_stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {"trade_count": 0, "profit_factor": None, "net_pnl": 0, "win_rate": 0}
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    pf = gp / gl if gl > 0 else (float("inf") if gp > 0 else 0)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trade_count": len(pnls),
        "profit_factor": round(pf, 4) if pf != float("inf") else None,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(wins / len(pnls), 4),
    }


def _pf_value(pnls: list[float]) -> float:
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    return gp / gl if gl > 0 else (999.0 if gp > 0 else 0)
