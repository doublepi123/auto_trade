"""Trade size impact analysis service.

Measures whether larger position sizes produce proportionally better or
worse risk-adjusted returns, detecting capacity constraints or sizing
misalignment.  Read-only.

Inspired by Freqtrade's stake-amount analysis and QuantConnect's capacity
estimation.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["SizeImpactService"]


class SizeImpactService:
    """PnL efficiency by position size quartile."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            symbol=symbol,
            lookback_days=lookback_days,
        )
        if mixed_error is not None:
            return mixed_error
        rows = [
            (
                abs(trade.entry_price * trade.quantity),
                trade.net_pnl,
                (
                    trade.net_pnl / abs(trade.entry_price * trade.quantity)
                    if trade.entry_price and trade.quantity
                    else 0.0
                ),
                trade.exit_at,
                trade.exit_order_id,
            )
            for trade in sample.trades
        ]
        if len(rows) < 8:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 8 closed trades with quantity data.",
            })

        # sort by quantity to form quartiles
        rows.sort(key=lambda r: (r[0], r[3], r[4]))
        n = len(rows)
        quartiles: list[list[tuple[float, float, float, Any, int]]] = [
            [] for _ in range(4)
        ]
        for index, row in enumerate(rows):
            quartiles[min(index * 4 // n, 3)].append(row)

        labels = ["Q1 (smallest)", "Q2", "Q3", "Q4 (largest)"]
        stats: list[dict[str, Any]] = []
        for label, group in zip(labels, quartiles):
            if not group:
                continue
            notionals = [row[0] for row in group]
            pnls = [row[1] for row in group]
            returns = [row[2] for row in group]
            total_pnl = sum(pnls)
            avg_notional = sum(notionals) / len(notionals)
            wins = sum(1 for p in pnls if p > 0)
            avg_return_pct = sum(returns) / len(returns) * 100
            stats.append(
                {
                    "quartile": label,
                    "trade_count": len(group),
                    "avg_entry_notional": round(avg_notional, 2),
                    "total_pnl": round(total_pnl, 2),
                    "avg_pnl": round(total_pnl / len(group), 2),
                    "win_rate": round(wins / len(group), 4),
                    "avg_return_pct": round(avg_return_pct, 4),
                }
            )

        # detect size efficiency trend
        if len(stats) >= 2:
            first_eff = stats[0]["avg_return_pct"]
            last_eff = stats[-1]["avg_return_pct"]
            relative_change = (last_eff - first_eff) / max(
                abs(first_eff),
                1e-9,
            )
            if relative_change > 0.2:
                trend = "increasing-returns"
            elif relative_change < -0.2:
                trend = "diminishing-returns"
            else:
                trend = "stable"
        else:
            trend = "insufficient"

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "quartiles": stats,
            "size_efficiency_trend": trend,
            "assessment": _assess(trend),
        })


def _assess(trend: str) -> str:
    if trend == "increasing-returns":
        return "Larger sizes produce better per-unit returns — capacity not yet constrained."
    if trend == "diminishing-returns":
        return "Larger sizes show diminishing per-unit returns — approaching capacity or slippage limits."
    if trend == "stable":
        return "Per-unit returns are stable across size quartiles — sizing is well-calibrated."
    return "Insufficient data to assess size efficiency."
