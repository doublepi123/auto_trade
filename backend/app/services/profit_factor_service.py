"""Profit factor decomposition service.

Breaks down the profit factor by symbol, time bucket, and trade size to
reveal which segments drive or drag overall edge.  Read-only.

Inspired by QuantStats' factor tearsheet and Edgewonk's edge decomposition.
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

__all__ = ["ProfitFactorService"]


class ProfitFactorService:
    """Decompose profit factor across multiple dimensions."""

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
        rows = [(trade.symbol, trade.net_pnl) for trade in sample.trades]
        if len(rows) < 5:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            })

        # overall
        gross_profit = sum(p for _, p in rows if p > 0)
        gross_loss = abs(sum(p for _, p in rows if p < 0))
        overall_pf, overall_pf_state = _profit_factor(gross_profit, gross_loss)

        # by symbol
        by_symbol: dict[str, list[float]] = defaultdict(list)
        for sym, pnl in rows:
            by_symbol[sym].append(pnl)

        symbol_breakdown = [
            {"segment": sym, **_pf_stats(pnls)}
            for sym, pnls in sorted(by_symbol.items(), key=lambda x: -_pf_value(x[1]))
        ]

        # by entry-notional rank; PnL magnitude is an outcome, not trade size.
        sized = sorted(
            sample.trades,
            key=lambda trade: (
                abs(trade.entry_price * trade.quantity),
                trade.exit_at,
                trade.exit_order_id,
            ),
        )
        size_groups: list[list[float]] = [[] for _ in range(4)]
        for index, trade in enumerate(sized):
            bucket = min(index * 4 // len(sized), 3)
            size_groups[bucket].append(trade.net_pnl)
        size_breakdown = [
            {"segment": label, **_pf_stats(group)}
            for label, group in zip(
                ("Q1 (smallest)", "Q2", "Q3", "Q4 (largest)"),
                size_groups,
            )
        ]

        # by win/loss contribution
        wins = sorted([p for _, p in rows if p > 0], reverse=True)
        losses = sorted([p for _, p in rows if p < 0])
        top3_win = sum(wins[:3]) if len(wins) >= 3 else sum(wins)
        top3_loss = sum(losses[:3]) if len(losses) >= 3 else sum(losses)

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "overall": {
                "profit_factor": (
                    round(overall_pf, 4) if overall_pf is not None else None
                ),
                "profit_factor_state": overall_pf_state,
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
        })


def _pf_stats(pnls: list[float]) -> dict[str, Any]:
    if not pnls:
        return {
            "trade_count": 0,
            "profit_factor": None,
            "profit_factor_state": "UNDEFINED",
            "net_pnl": 0,
            "win_rate": 0,
        }
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    pf, pf_state = _profit_factor(gp, gl)
    wins = sum(1 for p in pnls if p > 0)
    return {
        "trade_count": len(pnls),
        "profit_factor": round(pf, 4) if pf is not None else None,
        "profit_factor_state": pf_state,
        "net_pnl": round(sum(pnls), 2),
        "win_rate": round(wins / len(pnls), 4),
    }


def _pf_value(pnls: list[float]) -> float:
    gp = sum(p for p in pnls if p > 0)
    gl = abs(sum(p for p in pnls if p < 0))
    pf, pf_state = _profit_factor(gp, gl)
    if pf_state == "INFINITE":
        return float("inf")
    if pf_state == "UNDEFINED":
        return -1.0
    assert pf is not None
    return pf


def _profit_factor(gross_profit: float, gross_loss: float) -> tuple[float | None, str]:
    if gross_loss > 0:
        return gross_profit / gross_loss, "FINITE"
    if gross_profit > 0:
        return None, "INFINITE"
    return None, "UNDEFINED"
