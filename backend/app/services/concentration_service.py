"""Symbol concentration analysis service.

Computes Herfindahl-Hirschman Index and effective-N on PnL and trade-count
distribution across symbols to measure portfolio concentration risk.
Read-only.

Inspired by QuantConnect's portfolio risk models and Lean's exposure
analytics.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["ConcentrationService"]


class ConcentrationService:
    """HHI and effective-N concentration metrics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(self, lookback_days: int = 180) -> dict[str, Any]:
        rows = self._fetch(lookback_days)
        if len(rows) < 5:
            return {
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        pnl_by_symbol: dict[str, float] = defaultdict(float)
        count_by_symbol: dict[str, int] = defaultdict(int)
        for sym, pnl in rows:
            pnl_by_symbol[sym] += pnl
            count_by_symbol[sym] += 1

        total_abs_pnl = sum(abs(v) for v in pnl_by_symbol.values())
        total_count = sum(count_by_symbol.values())

        # HHI on absolute PnL share
        pnl_shares = [
            (abs(v) / total_abs_pnl) if total_abs_pnl > 0 else 0
            for v in pnl_by_symbol.values()
        ]
        hhi_pnl = sum(s**2 for s in pnl_shares)
        effective_n_pnl = 1.0 / hhi_pnl if hhi_pnl > 0 else 0

        # HHI on trade count share
        count_shares = [
            c / total_count if total_count > 0 else 0
            for c in count_by_symbol.values()
        ]
        hhi_count = sum(s**2 for s in count_shares)
        effective_n_count = 1.0 / hhi_count if hhi_count > 0 else 0

        # per-symbol breakdown
        symbols = sorted(pnl_by_symbol.keys())
        breakdown = [
            {
                "symbol": sym,
                "trade_count": count_by_symbol[sym],
                "total_pnl": round(pnl_by_symbol[sym], 2),
                "pnl_share": round(
                    abs(pnl_by_symbol[sym]) / total_abs_pnl, 4
                )
                if total_abs_pnl > 0
                else 0,
                "count_share": round(count_by_symbol[sym] / total_count, 4)
                if total_count > 0
                else 0,
            }
            for sym in symbols
        ]
        breakdown.sort(key=lambda x: x["pnl_share"], reverse=True)

        top_symbol = breakdown[0] if breakdown else None
        concentration_level = (
            "high" if hhi_pnl > 0.25 else "moderate" if hhi_pnl > 0.15 else "low"
        )

        return {
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "symbol_count": len(symbols),
            "hhi_pnl": round(hhi_pnl, 4),
            "effective_n_pnl": round(effective_n_pnl, 2),
            "hhi_count": round(hhi_count, 4),
            "effective_n_count": round(effective_n_count, 2),
            "concentration_level": concentration_level,
            "top_symbol": top_symbol,
            "breakdown": breakdown,
            "assessment": _assess(hhi_pnl, effective_n_pnl, concentration_level),
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.symbol, OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        rows = self._db.execute(stmt).all()
        return [(r[0], float(r[1])) for r in rows if r[1] is not None]


def _assess(hhi: float, eff_n: float, level: str) -> str:
    if level == "high":
        return f"High concentration (HHI={hhi:.3f}, effective N={eff_n:.1f}). PnL is dominated by few symbols — diversify."
    if level == "moderate":
        return f"Moderate concentration (HHI={hhi:.3f}, effective N={eff_n:.1f}). Acceptable but monitor single-symbol dominance."
    return f"Well-diversified (HHI={hhi:.3f}, effective N={eff_n:.1f}). No single symbol dominates PnL."
