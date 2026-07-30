"""Benchmark alpha/beta regression service.

Regresses daily strategy PnL against a simple market proxy (equal-weighted
average of all symbols' daily PnL) to estimate systematic exposure (beta)
and idiosyncratic edge (alpha).  Read-only.

Inspired by Zipline's risk module and QuantConnect's portfolio analytics.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["BenchmarkService"]


class BenchmarkService:
    """OLS alpha/beta of strategy returns vs internal market proxy."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        rows = self._fetch(lookback_days)
        if len(rows) < 10:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 10 trades.",
            }

        # build daily pnl by symbol
        by_date_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for sym, date_str, pnl in rows:
            by_date_symbol[date_str][sym] += pnl

        dates = sorted(by_date_symbol.keys())
        if len(dates) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(rows),
                "error": "Need at least 5 trading days.",
            }

        # market proxy = average daily pnl across all symbols
        market: list[float] = []
        strategy: list[float] = []
        for d in dates:
            day_pnls = by_date_symbol[d]
            mkt = sum(day_pnls.values()) / max(len(day_pnls), 1)
            market.append(mkt)
            if symbol:
                strategy.append(day_pnls.get(symbol, 0.0))
            else:
                strategy.append(sum(day_pnls.values()))

        n = len(dates)
        mean_m = sum(market) / n
        mean_s = sum(strategy) / n
        cov = sum((m - mean_m) * (s - mean_s) for m, s in zip(market, strategy))
        var_m = sum((m - mean_m) ** 2 for m in market)
        beta = cov / var_m if var_m > 0 else 0.0
        alpha = mean_s - beta * mean_m

        # R-squared
        ss_res = sum((s - (alpha + beta * m)) ** 2 for m, s in zip(market, strategy))
        ss_tot = sum((s - mean_s) ** 2 for s in strategy)
        r_squared = 1 - ss_res / ss_tot if ss_tot > 0 else 0.0

        # information ratio (alpha / tracking error)
        residuals = [s - (alpha + beta * m) for m, s in zip(market, strategy)]
        te = (sum(r**2 for r in residuals) / n) ** 0.5
        ir = alpha / te if te > 0 else 0.0

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(rows),
            "trading_days": n,
            "alpha": round(alpha, 4),
            "beta": round(beta, 4),
            "r_squared": round(max(r_squared, 0.0), 4),
            "information_ratio": round(ir, 4),
            "market_mean_daily": round(mean_m, 2),
            "strategy_mean_daily": round(mean_s, 2),
            "interpretation": _interpret(beta, alpha, r_squared),
        }

    def _fetch(self, days: int) -> list[tuple[str, str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.symbol, OrderRecord.filled_at, OrderRecord.net_pnl)
            .where(
                OrderRecord.net_pnl.is_not(None),
                OrderRecord.filled_at >= cutoff,
            )
            .order_by(OrderRecord.filled_at.asc())
        )
        rows = self._db.execute(stmt).all()
        return [
            (r[0], r[1].strftime("%Y-%m-%d") if r[1] else "", float(r[2]))
            for r in rows
            if r[2] is not None
        ]


def _interpret(beta: float, alpha: float, r2: float) -> str:
    parts: list[str] = []
    if beta > 1.2:
        parts.append("High systematic exposure (beta > 1.2)")
    elif beta < 0.5:
        parts.append("Low market sensitivity (beta < 0.5)")
    else:
        parts.append("Moderate market exposure")

    if alpha > 0:
        parts.append(f"positive idiosyncratic edge (+{alpha:.2f}/day)")
    else:
        parts.append(f"negative idiosyncratic drag ({alpha:.2f}/day)")

    if r2 > 0.7:
        parts.append("returns mostly explained by market")
    elif r2 < 0.3:
        parts.append("returns largely idiosyncratic")

    return "; ".join(parts) + "."
