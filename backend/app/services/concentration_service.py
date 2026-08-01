"""Symbol concentration analysis service.

Computes Herfindahl-Hirschman Index and effective-N on PnL and trade-count
distribution across symbols to measure portfolio concentration risk.
Read-only.

Inspired by QuantConnect's portfolio risk models and Lean's exposure
analytics.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["ConcentrationService"]


class ConcentrationService:
    """HHI and effective-N concentration metrics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(self, lookback_days: int = 180) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            symbol=None,
            lookback_days=lookback_days,
        )
        if mixed_error is not None:
            return mixed_error
        rows = [(trade.symbol, trade.net_pnl) for trade in sample.trades]
        if len(rows) < 5:
            return analytics_response(sample, {
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            })

        pnl_by_symbol: dict[str, float] = defaultdict(float)
        count_by_symbol: dict[str, int] = defaultdict(int)
        for sym, pnl in rows:
            pnl_by_symbol[sym] += pnl
            count_by_symbol[sym] += 1

        total_abs_pnl = sum(abs(v) for v in pnl_by_symbol.values())
        total_count = sum(count_by_symbol.values())

        # HHI on absolute PnL share
        pnl_shares = [
            (abs(v) / total_abs_pnl) if total_abs_pnl > 0 else None
            for v in pnl_by_symbol.values()
        ]
        hhi_pnl = (
            sum(share**2 for share in pnl_shares if share is not None)
            if total_abs_pnl > 0
            else None
        )
        effective_n_pnl = (
            1.0 / hhi_pnl
            if hhi_pnl is not None and hhi_pnl > 0
            else None
        )

        # HHI on trade count share
        count_shares = [
            c / total_count if total_count > 0 else 0
            for c in count_by_symbol.values()
        ]
        hhi_count = sum(s**2 for s in count_shares)
        effective_n_count = 1.0 / hhi_count if hhi_count > 0 else 0

        # per-symbol breakdown
        symbols = sorted(pnl_by_symbol.keys())
        breakdown: list[dict[str, Any]] = [
            {
                "symbol": sym,
                "trade_count": count_by_symbol[sym],
                "total_pnl": round(pnl_by_symbol[sym], 2),
                "pnl_share": round(
                    abs(pnl_by_symbol[sym]) / total_abs_pnl, 4
                )
                if total_abs_pnl > 0
                else None,
                "count_share": round(count_by_symbol[sym] / total_count, 4)
                if total_count > 0
                else 0,
            }
            for sym in symbols
        ]
        breakdown.sort(
            key=lambda item: (-(item["pnl_share"] or 0), item["symbol"]),
        )

        if hhi_pnl is None:
            top_symbol = None
            concentration_level = "unavailable"
            analysis_status = "UNAVAILABLE"
        else:
            top_symbol = breakdown[0] if breakdown else None
            concentration_level = (
                "high"
                if hhi_pnl > 0.25
                else "moderate"
                if hhi_pnl > 0.15
                else "low"
            )
            analysis_status = "READY"

        return analytics_response(sample, {
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "analysis_status": analysis_status,
            "symbol_count": len(symbols),
            "hhi_pnl": round(hhi_pnl, 4) if hhi_pnl is not None else None,
            "effective_n_pnl": (
                round(effective_n_pnl, 2)
                if effective_n_pnl is not None
                else None
            ),
            "hhi_count": round(hhi_count, 4),
            "effective_n_count": round(effective_n_count, 2),
            "concentration_level": concentration_level,
            "top_symbol": top_symbol,
            "breakdown": breakdown,
            "assessment": _assess(hhi_pnl, effective_n_pnl, concentration_level),
        })


def _assess(hhi: float | None, eff_n: float | None, level: str) -> str:
    if level == "unavailable":
        return (
            "PnL concentration is unavailable because every symbol has zero "
            "net PnL; trade-count concentration remains available."
        )
    assert hhi is not None and eff_n is not None
    if level == "high":
        return f"High concentration (HHI={hhi:.3f}, effective N={eff_n:.1f}). PnL is dominated by few symbols — diversify."
    if level == "moderate":
        return f"Moderate concentration (HHI={hhi:.3f}, effective N={eff_n:.1f}). Acceptable but monitor single-symbol dominance."
    return f"Well-diversified (HHI={hhi:.3f}, effective N={eff_n:.1f}). No single symbol dominates PnL."
