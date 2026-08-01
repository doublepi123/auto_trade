"""Symbol net-return momentum ranking service.

Ranks traded symbols by the slope of cumulative per-trade net returns to
identify which symbols are trending favorably or unfavorably without position
size determining the rank. Native-currency PnL remains descriptive only.
Read-only.

Inspired by QuantConnect's momentum ranking and Lean's cross-sectional
signal generation.
"""
from __future__ import annotations

from collections import defaultdict
import math
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["MomentumRankingService"]


class MomentumRankingService:
    """Cross-sectional net-return momentum ranking across symbols."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def rank(
        self, lookback_days: int = 90, min_trades: int = 3
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=lookback_days,
        )
        rows = [
            (
                trade.symbol,
                trade.net_pnl,
                trade.net_pnl / (trade.entry_price * trade.quantity),
            )
            for trade in sample.trades
            if (
                math.isfinite(trade.entry_price)
                and math.isfinite(trade.quantity)
                and trade.entry_price > 0
                and trade.quantity > 0
            )
        ]
        currency_error = mixed_currency_error(
            sample,
            payload={
                "lookback_days": lookback_days,
                "sample_size": len(rows),
            },
        )
        if currency_error is not None:
            return currency_error
        if len(rows) < 5:
            return analytics_response(
                sample,
                {
                    "lookback_days": lookback_days,
                    "sample_size": len(rows),
                    "error": "Need at least 5 closed trades.",
                },
            )

        # group by symbol, preserving time order
        by_symbol: dict[str, list[tuple[float, float]]] = defaultdict(list)
        for sym, pnl, net_return in rows:
            by_symbol[sym].append((pnl, net_return))

        rankings: list[dict[str, Any]] = []
        for sym, outcomes in by_symbol.items():
            if len(outcomes) < min_trades:
                continue
            pnls = [pnl for pnl, _net_return in outcomes]
            returns = [net_return for _pnl, net_return in outcomes]
            total = sum(pnls)
            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0)

            # Ranking on cumulative net return avoids treating a larger
            # position size as stronger symbol momentum. Native-currency PnL
            # remains descriptive only.
            cum = 0.0
            cum_series: list[float] = []
            for net_return in returns:
                cum += net_return
                cum_series.append(cum)

            slope = _slope(cum_series)

            # recent vs older performance
            half = n // 2
            recent = sum(returns[-half:])
            older = sum(returns[:half])
            acceleration = recent - older

            rankings.append(
                {
                    "symbol": sym,
                    "trade_count": n,
                    "total_pnl": round(total, 2),
                    "win_rate": round(wins / n, 4),
                    "momentum_slope": round(slope, 8),
                    "recent_return": round(recent, 8),
                    "older_return": round(older, 8),
                    "return_acceleration": round(acceleration, 8),
                }
            )

        rankings.sort(
            key=lambda item: (-item["momentum_slope"], item["symbol"])
        )
        for i, r in enumerate(rankings):
            r["rank"] = i + 1

        top = rankings[0] if rankings else None
        bottom = rankings[-1] if rankings else None

        return analytics_response(
            sample,
            {
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "qualifying_symbols": len(rankings),
                "rankings": rankings,
                "top_momentum": top,
                "bottom_momentum": bottom,
            },
        )


def _slope(values: list[float]) -> float:
    n = len(values)
    if n < 2:
        return 0.0
    x_mean = (n - 1) / 2.0
    y_mean = sum(values) / n
    num = sum((i - x_mean) * (v - y_mean) for i, v in enumerate(values))
    den = sum((i - x_mean) ** 2 for i in range(n))
    return num / den if den > 0 else 0.0
