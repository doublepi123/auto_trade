"""Lookahead bias analysis service.

Detects potential lookahead bias by comparing strategy trade statistics
across chronologically sliced data windows.  If removing the most recent
N% of trades materially changes win-rate or PnL, the strategy may be
implicitly relying on future information.

Read-only and side-effect free — never writes to the database.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["LookaheadAnalysisService"]

_DEFAULT_SLICE_PCTS = [100.0, 90.0, 80.0, 70.0, 60.0, 50.0]
_BIAS_THRESHOLD = 0.15  # 15 % relative change flags bias


class LookaheadAnalysisService:
    """Compare trade stats across chronological slices to detect bias."""

    def __init__(self, db: Session) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def analyze(
        self,
        symbol: str | None = None,
        lookback_days: int = 90,
        slice_pcts: list[float] | None = None,
    ) -> dict[str, Any]:
        """Run lookahead bias analysis and return a JSON-friendly dict."""
        lookback_days = max(1, int(lookback_days))
        pcts = sorted(
            (slice_pcts or _DEFAULT_SLICE_PCTS),
            reverse=True,
        )
        rows = self._fetch_exits(symbol=symbol, days=lookback_days)
        baseline = self._slice_stats(rows, 100.0)

        slices: list[dict[str, Any]] = []
        max_delta = 0.0
        for pct in pcts:
            stats = self._slice_stats(rows, pct)
            wr_delta = abs(stats["win_rate"] - baseline["win_rate"])
            pnl_delta = (
                abs(stats["avg_pnl"] - baseline["avg_pnl"])
                / max(abs(baseline["avg_pnl"]), 1.0)
            )
            consistency = self._signal_consistency(rows, pct)
            slices.append(
                {
                    "pct": pct,
                    "trade_count": stats["trade_count"],
                    "win_rate": round(stats["win_rate"], 4),
                    "total_pnl": round(stats["total_pnl"], 2),
                    "avg_pnl": round(stats["avg_pnl"], 2),
                    "signal_consistency": round(consistency, 4),
                    "win_rate_delta": round(wr_delta, 4),
                    "pnl_delta": round(pnl_delta, 4),
                }
            )
            max_delta = max(max_delta, wr_delta, pnl_delta)

        has_bias = max_delta > _BIAS_THRESHOLD
        bias_score = round(min(max_delta, 1.0), 4)
        if has_bias:
            recommendation = (
                "Strategy shows sensitivity to data window — "
                "review indicators for potential future-data leakage."
            )
        else:
            recommendation = (
                "Strategy statistics are stable across data slices — "
                "no evidence of lookahead bias."
            )

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "total_exits": len(rows),
            "baseline": {
                "trade_count": baseline["trade_count"],
                "win_rate": round(baseline["win_rate"], 4),
                "total_pnl": round(baseline["total_pnl"], 2),
                "avg_pnl": round(baseline["avg_pnl"], 2),
            },
            "slices": slices,
            "has_bias": has_bias,
            "bias_score": bias_score,
            "recommendation": recommendation,
        }

    # ------------------------------------------------------------------
    # data access
    # ------------------------------------------------------------------

    def _fetch_exits(
        self, symbol: str | None, days: int
    ) -> list[OrderRecord]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        return list(self._db.scalars(stmt).all())

    # ------------------------------------------------------------------
    # computation
    # ------------------------------------------------------------------

    @staticmethod
    def _slice_stats(rows: list[OrderRecord], pct: float) -> dict[str, Any]:
        if not rows:
            return {
                "trade_count": 0,
                "win_rate": 0.0,
                "total_pnl": 0.0,
                "avg_pnl": 0.0,
            }
        n = max(1, int(len(rows) * pct / 100.0))
        subset = rows[:n]
        pnls = [
            float(r.net_pnl) if r.net_pnl is not None else 0.0
            for r in subset
        ]
        total = sum(pnls)
        wins = sum(1 for p in pnls if p > 0)
        count = len(pnls)
        return {
            "trade_count": count,
            "win_rate": (wins / count) if count else 0.0,
            "total_pnl": total,
            "avg_pnl": (total / count) if count else 0.0,
        }

    @staticmethod
    def _signal_consistency(rows: list[OrderRecord], pct: float) -> float:
        """Fraction of baseline trades that appear in the sliced window."""
        if not rows:
            return 1.0
        n = max(1, int(len(rows) * pct / 100.0))
        return n / len(rows)
