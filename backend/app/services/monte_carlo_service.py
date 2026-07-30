"""Monte Carlo simulation service.

Resamples historical closed-trade PnLs with replacement to estimate the
distribution of future cumulative returns, ruin probability, and confidence
intervals.  Read-only — never writes to the database.

Inspired by QuantStats' bootstrap tearsheet and VectorBT's simulation module.
"""
from __future__ import annotations

import random
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["MonteCarloService"]


class MonteCarloService:
    """Bootstrap resampling of trade PnLs for forward-looking estimates."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def simulate(
        self,
        symbol: str | None = None,
        lookback_days: int = 180,
        n_simulations: int = 1000,
        n_trades: int | None = None,
        seed: int = 42,
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 5:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "n_simulations": 0,
                "error": "Need at least 5 closed trades to run simulation.",
            }

        rng = random.Random(seed)
        sim_trades = n_trades or len(pnls)
        final_pnls: list[float] = []
        max_drawdowns: list[float] = []
        ruin_count = 0
        ruin_threshold = -sum(abs(p) for p in pnls) * 0.5

        for _ in range(n_simulations):
            path = [rng.choice(pnls) for _ in range(sim_trades)]
            cum = 0.0
            peak = 0.0
            max_dd = 0.0
            for p in path:
                cum += p
                peak = max(peak, cum)
                dd = peak - cum
                max_dd = max(max_dd, dd)
            final_pnls.append(cum)
            max_drawdowns.append(max_dd)
            if cum < ruin_threshold:
                ruin_count += 1

        final_pnls.sort()
        max_drawdowns.sort()

        def percentile(data: list[float], pct: float) -> float:
            idx = int(len(data) * pct / 100.0)
            idx = min(idx, len(data) - 1)
            return round(data[idx], 2)

        mean_pnl = sum(final_pnls) / len(final_pnls)
        sample_mean = sum(pnls) / len(pnls)
        sample_wr = sum(1 for p in pnls if p > 0) / len(pnls)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": len(pnls),
            "n_simulations": n_simulations,
            "sim_trades": sim_trades,
            "sample_stats": {
                "mean_pnl": round(sample_mean, 2),
                "win_rate": round(sample_wr, 4),
                "total_pnl": round(sum(pnls), 2),
                "best_trade": round(max(pnls), 2),
                "worst_trade": round(min(pnls), 2),
            },
            "final_pnl_distribution": {
                "mean": round(mean_pnl, 2),
                "median": percentile(final_pnls, 50),
                "p5": percentile(final_pnls, 5),
                "p25": percentile(final_pnls, 25),
                "p75": percentile(final_pnls, 75),
                "p95": percentile(final_pnls, 95),
                "min": round(final_pnls[0], 2),
                "max": round(final_pnls[-1], 2),
            },
            "max_drawdown_distribution": {
                "mean": round(sum(max_drawdowns) / len(max_drawdowns), 2),
                "median": percentile(max_drawdowns, 50),
                "p95": percentile(max_drawdowns, 95),
                "max": round(max_drawdowns[-1], 2),
            },
            "ruin_probability": round(ruin_count / n_simulations, 4),
            "profit_probability": round(
                sum(1 for p in final_pnls if p > 0) / n_simulations, 4
            ),
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        from datetime import datetime, timedelta, timezone

        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]
