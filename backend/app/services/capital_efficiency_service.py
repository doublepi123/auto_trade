"""Capital efficiency analysis service.

Measures return per unit of capital deployed, turnover ratio, and idle
capital drag to assess how efficiently the strategy uses available funds.
Read-only.

Inspired by QuantConnect's portfolio analytics and Lean's capital
utilization metrics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["CapitalEfficiencyService"]


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
        rows = self._fetch(symbol, lookback_days)
        if len(rows) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        pnls = [pnl for _, pnl in rows]
        quantities = [qty for qty, _ in rows]
        total_pnl = sum(pnls)
        total_traded_value = sum(
            abs(qty) * 100 for qty in quantities  # approximate notional
        )

        # return on capital
        roc = total_pnl / capital_base if capital_base > 0 else 0
        annualized_roc = roc * (365.0 / max(lookback_days, 1))

        # turnover: total traded / capital
        turnover = total_traded_value / capital_base if capital_base > 0 else 0

        # efficiency: pnl per unit traded
        pnl_per_traded = total_pnl / total_traded_value if total_traded_value > 0 else 0

        # win/loss capital allocation
        win_capital = sum(abs(q) * 100 for q, p in rows if p > 0)
        loss_capital = sum(abs(q) * 100 for q, p in rows if p < 0)
        capital_efficiency = (
            win_capital / (win_capital + loss_capital)
            if (win_capital + loss_capital) > 0
            else 0
        )

        # daily utilization estimate
        active_days = len(set(
            ts.strftime("%Y-%m-%d") for ts, _ in rows if ts
        ))
        utilization = active_days / max(lookback_days, 1)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "capital_base": capital_base,
            "total_pnl": round(total_pnl, 2),
            "return_on_capital": round(roc * 100, 2),
            "annualized_roc": round(annualized_roc * 100, 2),
            "turnover_ratio": round(turnover, 2),
            "pnl_per_unit_traded": round(pnl_per_traded, 6),
            "capital_efficiency": round(capital_efficiency, 4),
            "active_days": active_days,
            "utilization_rate": round(utilization, 4),
            "assessment": _assess(annualized_roc, capital_efficiency, utilization),
        }

    def _fetch(
        self, symbol: str | None, days: int
    ) -> list[tuple[datetime | None, float, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(
            OrderRecord.filled_at,
            OrderRecord.net_pnl,
            OrderRecord.quantity,
        ).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.execute(stmt).all()
        return [
            (r[0], float(r[1]), float(r[2]) if r[2] else 0)
            for r in rows
            if r[1] is not None
        ]


def _assess(roc: float, eff: float, util: float) -> str:
    parts: list[str] = []
    if roc > 20:
        parts.append("Strong annualized return on capital")
    elif roc > 0:
        parts.append("Positive but modest return on capital")
    else:
        parts.append("Negative return — capital is being destroyed")

    if eff > 0.6:
        parts.append("capital allocated efficiently to winners")
    else:
        parts.append("capital allocation skewed toward losers")

    if util < 0.2:
        parts.append("low market utilization — capital mostly idle")
    elif util > 0.8:
        parts.append("high utilization — ensure not overtrading")

    return "; ".join(parts) + "."
