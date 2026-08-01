"""Profit concentration (Pareto) analysis service.

Measures how concentrated profits are in a small fraction of winning
trades: top-N% trade share of gross profit, a Lorenz-style cumulative
curve, and a Gini coefficient over winning-trade PnL.  Read-only.

Inspired by QuantStats' Pareto-style profit concentration reports.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["ProfitConcentrationService"]

_LEVELS = (0.01, 0.05, 0.10, 0.20, 0.50, 1.0)


class ProfitConcentrationService:
    """Trade-level profit concentration analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
        )
        rows = [trade.net_pnl for trade in sample.trades]
        currency_error = mixed_currency_error(
            sample,
            payload={"days": days, "sample_size": len(rows)},
        )
        if currency_error is not None:
            return currency_error
        if len(rows) < 5:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(rows),
                    "error": "Need at least 5 closed trades.",
                },
            )

        wins = sorted((p for p in rows if p > 0), reverse=True)
        losses = [p for p in rows if p < 0]
        total_win = sum(wins)
        total_loss = sum(losses)

        if not wins or total_win <= 0:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(rows),
                    "error": "No winning trades in window.",
                },
            )

        n_wins = len(wins)
        pareto: list[dict[str, float]] = []
        for level in _LEVELS:
            k = max(1, math.ceil(n_wins * level))
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

        return analytics_response(
            sample,
            {
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
            },
        )


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
