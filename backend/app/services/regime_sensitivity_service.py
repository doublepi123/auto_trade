"""Regime sensitivity analysis service.

Splits trades into high/low prior-outcome variability states using the rolling
standard deviation of PnL already closed before each entry, then compares
subsequent outcomes. This is not a market-volatility signal. Read-only.

Inspired by VectorBT's regime analytics and QuantStats' conditional
performance tearsheet.
"""
from __future__ import annotations

import math
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["RegimeSensitivityService"]

_REGIME_BASIS = "PRIOR_CLOSED_TRADE_PNL_VOLATILITY"


class RegimeSensitivityService:
    """Compare outcomes across prior closed-trade PnL variability states."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        window: int = 20,
    ) -> dict[str, Any]:
        normalized_symbol = (
            symbol.strip().upper() if symbol and symbol.strip() else None
        )
        sample = load_analytics_trade_sample(
            self._db,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        trades = sorted(
            sample.trades,
            key=lambda trade: (
                trade.entry_at,
                trade.entry_order_id,
                trade.exit_at,
                trade.exit_order_id,
            ),
        )
        currency_error = mixed_currency_error(
            sample,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
            payload={
                "symbol": normalized_symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(trades),
                "window": window,
                "regime_basis": _REGIME_BASIS,
            },
        )
        if currency_error is not None:
            return currency_error
        if len(trades) < window * 2:
            return analytics_response(
                sample,
                {
                    "symbol": normalized_symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(trades),
                    "window": window,
                    "regime_basis": _REGIME_BASIS,
                    "error": f"Need at least {window * 2} trades.",
                },
            )

        # The current outcome must not choose its own regime. For each entry,
        # volatility uses only outcomes that closed strictly before entry; its
        # high/low threshold is the expanding median of earlier volatility
        # estimates, also known before entry.
        closed = sorted(
            trades,
            key=lambda trade: (trade.exit_at, trade.exit_order_id),
        )
        known_pnls: list[float] = []
        historical_vols: list[float] = []
        low_vol_pnls: list[float] = []
        high_vol_pnls: list[float] = []
        closed_index = 0
        for trade in trades:
            while (
                closed_index < len(closed)
                and closed[closed_index].exit_at < trade.entry_at
            ):
                known_pnls.append(closed[closed_index].net_pnl)
                closed_index += 1
            if len(known_pnls) < window:
                continue
            prior_window = known_pnls[-window:]
            prior_mean = sum(prior_window) / window
            prior_variance = sum(
                (pnl - prior_mean) ** 2 for pnl in prior_window
            ) / window
            volatility = math.sqrt(prior_variance)
            if not historical_vols:
                historical_vols.append(volatility)
                continue
            threshold = median(historical_vols)
            if volatility <= threshold:
                low_vol_pnls.append(trade.net_pnl)
            else:
                high_vol_pnls.append(trade.net_pnl)
            historical_vols.append(volatility)

        if (
            len(historical_vols) < 2
            or math.isclose(
                min(historical_vols),
                max(historical_vols),
                rel_tol=1e-12,
                abs_tol=1e-12,
            )
        ):
            return analytics_response(
                sample,
                {
                    "symbol": normalized_symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(trades),
                    "window": window,
                    "regime_basis": _REGIME_BASIS,
                    "error": "No causal volatility variation detected.",
                },
            )
        classified_count = len(low_vol_pnls) + len(high_vol_pnls)
        if not low_vol_pnls or not high_vol_pnls:
            return analytics_response(
                sample,
                {
                    "symbol": normalized_symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(trades),
                    "window": window,
                    "regime_basis": _REGIME_BASIS,
                    "classified_trades": classified_count,
                    "error": (
                        "Need causally classifiable trades in both prior-PnL "
                        "variability states."
                    ),
                },
            )
        median_vol = median(historical_vols)

        def _regime_stats(pnls_list: list[float], label: str) -> dict[str, Any]:
            if not pnls_list:
                return {"regime": label, "trade_count": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0, "sharpe": 0}
            n = len(pnls_list)
            wins = sum(1 for p in pnls_list if p > 0)
            total = sum(pnls_list)
            mean = total / n
            var = sum((p - mean) ** 2 for p in pnls_list) / n
            std = math.sqrt(var) if var > 0 else 0
            sharpe = (mean / std * math.sqrt(252)) if std > 0 else 0
            return {
                "regime": label,
                "trade_count": n,
                "win_rate": round(wins / n, 4),
                "avg_pnl": round(mean, 2),
                "total_pnl": round(total, 2),
                "sharpe": round(sharpe, 4),
            }

        low_stats = _regime_stats(low_vol_pnls, "low_volatility")
        high_stats = _regime_stats(high_vol_pnls, "high_volatility")

        # regime sensitivity: difference in Sharpe
        sensitivity = high_stats["sharpe"] - low_stats["sharpe"]

        return analytics_response(
            sample,
            {
                "symbol": normalized_symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(trades),
                "classified_trades": classified_count,
                "window": window,
                "regime_basis": _REGIME_BASIS,
                "median_volatility": round(median_vol, 4),
                "regimes": [low_stats, high_stats],
                "sensitivity": round(sensitivity, 4),
                "interpretation": _interpret(
                    sensitivity,
                    low_stats,
                    high_stats,
                ),
            },
        )


def _interpret(sens: float, low: dict, high: dict) -> str:
    if sens > 0.5:
        return (
            "Strategy outcomes have higher Sharpe after periods of high "
            "closed-trade PnL variability. This is not a market-volatility "
            "signal."
        )
    if sens < -0.5:
        return (
            "Strategy outcomes have higher Sharpe after periods of low "
            "closed-trade PnL variability. This is not a market-volatility "
            "signal."
        )
    return (
        "Strategy outcomes are relatively stable across prior closed-trade "
        "PnL variability states."
    )
