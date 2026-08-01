"""Capital efficiency analysis service.

Measures return per unit of supplied capital, turnover, and capital-time
observed in quality-gated closed round trips. Open positions are deliberately
outside this read-only evidence set.

Inspired by QuantConnect's portfolio analytics and Lean's capital
utilization metrics.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
    trade_local_day,
)

__all__ = ["CapitalEfficiencyService"]

_EVIDENCE_SCOPE = "CLOSED_ROUND_TRIPS_ONLY"
_EVIDENCE_NOTE = (
    "Capital-time utilization covers only FIFO-paired round trips that closed "
    "in the requested window; open positions are not included."
)


class CapitalEfficiencyService:
    """Return-on-capital and utilization metrics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        capital_base: float = 10000.0,
    ) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            symbol=symbol,
            lookback_days=lookback_days,
            include_excursions=False,
        )
        mixed_error = mixed_currency_error(
            sample,
            payload={
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(sample.trades),
                "capital_base": capital_base,
                "capital_base_currency": None,
                "evidence_scope": _EVIDENCE_SCOPE,
                "evidence_note": _EVIDENCE_NOTE,
            },
        )
        if mixed_error is not None:
            return mixed_error
        trades = sample.trades
        if len(trades) < 5:
            return analytics_response(
                sample,
                {
                    "symbol": symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(trades),
                    "capital_base": capital_base,
                    "capital_base_currency": sample.currency,
                    "evidence_scope": _EVIDENCE_SCOPE,
                    "evidence_note": _EVIDENCE_NOTE,
                    "error": "Need at least 5 closed trades.",
                },
            )

        pnls = [trade.net_pnl for trade in trades]
        total_pnl = sum(pnls)
        total_traded_value = sum(
            trade.quantity * (trade.entry_price + trade.exit_price)
            for trade in trades
        )

        # return on capital
        roc = total_pnl / capital_base if capital_base > 0 else 0
        annualized_roc = roc * (365.0 / max(lookback_days, 1))

        # turnover: total traded / capital
        turnover = total_traded_value / capital_base if capital_base > 0 else 0

        # efficiency: pnl per unit traded
        pnl_per_traded = total_pnl / total_traded_value if total_traded_value > 0 else 0

        total_entry_notional = sum(
            trade.quantity * trade.entry_price
            for trade in trades
        )
        winning_entry_notional = sum(
            trade.quantity * trade.entry_price
            for trade in trades
            if trade.net_pnl > 0
        )
        winning_entry_notional_share = (
            winning_entry_notional / total_entry_notional
            if total_entry_notional > 0
            else 0.0
        )

        # Capital-time is intentionally limited to round trips that close in
        # this sample. It does not infer exposure for still-open positions.
        capital_time = sum(
            trade.quantity * trade.entry_price * trade.holding_seconds
            for trade in trades
        )
        window_seconds = max(
            (sample.to_dt - sample.from_dt).total_seconds(),
            1.0,
        )
        average_closed_round_trip_capital = capital_time / window_seconds
        capital_time_utilization_rate = (
            average_closed_round_trip_capital / capital_base
            if capital_base > 0
            else 0.0
        )

        # Preserve the former date-count metric under an explicit name. It is
        # not a measure of how long or how much capital was deployed.
        exit_active_days = len(
            {
                trade_local_day(trade.symbol, trade.exit_at)
                for trade in trades
            }
        )
        exit_active_day_rate = exit_active_days / max(lookback_days, 1)

        return analytics_response(
            sample,
            {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(trades),
                "capital_base": capital_base,
                "capital_base_currency": sample.currency,
                "evidence_scope": _EVIDENCE_SCOPE,
                "evidence_note": _EVIDENCE_NOTE,
                "total_pnl": round(total_pnl, 2),
                "return_on_capital": round(roc * 100, 2),
                "annualized_roc": round(annualized_roc * 100, 2),
                "turnover_ratio": round(turnover, 2),
                "pnl_per_unit_traded": round(pnl_per_traded, 6),
                "total_entry_notional": round(total_entry_notional, 2),
                "winning_entry_notional_share": round(
                    winning_entry_notional_share,
                    4,
                ),
                "average_closed_round_trip_capital": round(
                    average_closed_round_trip_capital,
                    2,
                ),
                "capital_time_utilization_rate": round(
                    capital_time_utilization_rate,
                    6,
                ),
                # Backwards-compatible field; its semantics are now the
                # capital-time rate above, not the former exit-date ratio.
                "utilization_rate": round(capital_time_utilization_rate, 6),
                "exit_active_days": exit_active_days,
                "exit_active_day_rate": round(exit_active_day_rate, 4),
                "assessment": _assess(
                    annualized_roc,
                    capital_time_utilization_rate,
                ),
            },
        )


def _assess(roc: float, utilization: float) -> str:
    parts: list[str] = []
    if roc > 0.2:
        parts.append("Strong annualized return on capital")
    elif roc > 0:
        parts.append("Positive but modest return on capital")
    else:
        parts.append("Negative return on the supplied capital base")

    if utilization < 0.2:
        parts.append(
            "closed-round-trip capital-time utilization was below 20%"
        )
    elif utilization > 1.0:
        parts.append(
            "closed-round-trip average exposure exceeded the supplied capital base"
        )
    else:
        parts.append(
            "closed-round-trip capital-time utilization was "
            f"{utilization:.1%}"
        )

    return "; ".join(parts) + "; open positions are not included."
