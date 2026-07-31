"""Win/loss asymmetry analysis service.

Provides a detailed breakdown of the asymmetry between winning and losing
trades: magnitude distributions, tail behavior, and conditional patterns.
Read-only.

Inspired by QuantStats' win/loss analysis and Edgewonk's trade anatomy.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["AsymmetryService"]


class AsymmetryService:
    """Detailed win/loss distribution asymmetry analysis."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 10:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 10 closed trades.",
            }

        wins = sorted([p for p in pnls if p > 0], reverse=True)
        losses = sorted([p for p in pnls if p < 0])
        zeros = sum(1 for p in pnls if p == 0)

        def _stats(vals: list[float], label: str) -> dict[str, Any]:
            if not vals:
                return {"label": label, "count": 0, "total": 0, "avg": 0, "median": 0, "max": 0, "min": 0, "top3_share": 0}
            total = sum(vals)
            n = len(vals)
            sorted_v = sorted(vals)
            top3 = sum(sorted(vals, key=abs, reverse=True)[:3])
            return {
                "label": label,
                "count": n,
                "total": round(total, 2),
                "avg": round(total / n, 2),
                "median": round(sorted_v[n // 2], 2),
                "max": round(max(vals, key=abs), 2),
                "min": round(min(vals, key=abs), 2),
                "top3_share": round(abs(top3) / abs(total), 4) if total != 0 else 0,
            }

        win_stats = _stats(wins, "wins")
        loss_stats = _stats(losses, "losses")

        # asymmetry ratio: avg_win / avg_loss
        avg_win = win_stats["avg"] if win_stats["count"] > 0 else 0
        avg_loss = abs(loss_stats["avg"]) if loss_stats["count"] > 0 else 1
        asymmetry_ratio = avg_win / avg_loss if avg_loss > 0 else float("inf")

        # profit/loss concentration
        total_win = sum(wins) if wins else 0
        total_loss = abs(sum(losses)) if losses else 0

        # conditional: what happens after a big win/loss
        big_win_threshold = wins[len(wins) // 4] if len(wins) >= 4 else (wins[0] if wins else 0)
        big_loss_threshold = losses[len(losses) // 4] if len(losses) >= 4 else (losses[0] if losses else 0)

        after_big_win = [pnls[i + 1] for i in range(len(pnls) - 1) if pnls[i] >= big_win_threshold and big_win_threshold > 0]
        after_big_loss = [pnls[i + 1] for i in range(len(pnls) - 1) if pnls[i] <= big_loss_threshold and big_loss_threshold < 0]

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "win_stats": win_stats,
            "loss_stats": loss_stats,
            "breakeven_count": zeros,
            "asymmetry_ratio": round(asymmetry_ratio, 4) if asymmetry_ratio != float("inf") else None,
            "total_win": round(total_win, 2),
            "total_loss": round(total_loss, 2),
            "net_edge": round(total_win - total_loss, 2),
            "conditional": {
                "after_big_win_avg": round(sum(after_big_win) / len(after_big_win), 2) if after_big_win else None,
                "after_big_win_count": len(after_big_win),
                "after_big_loss_avg": round(sum(after_big_loss) / len(after_big_loss), 2) if after_big_loss else None,
                "after_big_loss_count": len(after_big_loss),
            },
            "assessment": _assess(asymmetry_ratio, win_stats["count"], loss_stats["count"]),
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


def _assess(ratio: float, wins: int, losses: int) -> str:
    if ratio == float("inf"):
        return "No losses recorded — asymmetry undefined."
    if ratio > 2.0:
        return f"Strong positive asymmetry (ratio={ratio:.2f}) — wins are much larger than losses. Classic trend-following profile."
    if ratio > 1.0:
        return f"Moderate positive asymmetry (ratio={ratio:.2f}) — wins exceed losses on average."
    if ratio > 0.5:
        return f"Negative asymmetry (ratio={ratio:.2f}) — losses exceed wins. High win-rate needed to compensate."
    return f"Severe negative asymmetry (ratio={ratio:.2f}) — losses dominate. Review stop-loss and exit logic."
