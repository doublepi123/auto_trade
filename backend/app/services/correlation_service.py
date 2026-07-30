"""Symbol return correlation service.

Computes pairwise Pearson correlation of daily realized PnL across symbols
to surface concentration risk and diversification quality.  Read-only.

Inspired by QuantStats tearsheet correlation matrix and VectorBT's
portfolio analytics module.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["CorrelationService"]


class CorrelationService:
    """Pairwise daily-PnL correlation across traded symbols."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def compute(
        self, lookback_days: int = 90, min_trades: int = 3
    ) -> dict[str, Any]:
        rows = self._fetch(lookback_days)
        # group by symbol -> date -> pnl
        by_symbol: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
        for symbol, date_str, pnl in rows:
            by_symbol[symbol][date_str] += pnl

        # filter symbols with enough trading days
        symbols = sorted(
            s for s, days in by_symbol.items() if len(days) >= min_trades
        )
        if len(symbols) < 2:
            return {
                "lookback_days": lookback_days,
                "symbols": symbols,
                "matrix": [],
                "pairs": [],
                "note": "Need at least 2 symbols with sufficient data.",
            }

        # build aligned return vectors (only overlapping dates)
        all_dates = sorted(
            set().union(*(by_symbol[s].keys() for s in symbols))
        )
        vectors: dict[str, list[float]] = {
            s: [by_symbol[s].get(d, 0.0) for d in all_dates] for s in symbols
        }

        n = len(symbols)
        matrix: list[list[float]] = [[1.0] * n for _ in range(n)]
        pairs: list[dict[str, Any]] = []

        for i in range(n):
            for j in range(i + 1, n):
                corr = self._pearson(vectors[symbols[i]], vectors[symbols[j]])
                matrix[i][j] = round(corr, 4)
                matrix[j][i] = round(corr, 4)
                pairs.append(
                    {
                        "symbol_a": symbols[i],
                        "symbol_b": symbols[j],
                        "correlation": round(corr, 4),
                    }
                )

        pairs.sort(key=lambda p: abs(p["correlation"]), reverse=True)

        avg_abs = (
            sum(abs(p["correlation"]) for p in pairs) / len(pairs) if pairs else 0.0
        )

        return {
            "lookback_days": lookback_days,
            "symbols": symbols,
            "matrix": matrix,
            "pairs": pairs,
            "avg_abs_correlation": round(avg_abs, 4),
            "diversification_score": round(1.0 - min(avg_abs, 1.0), 4),
        }

    def _fetch(self, days: int) -> list[tuple[str, str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(
                OrderRecord.symbol,
                OrderRecord.filled_at,
                OrderRecord.net_pnl,
            )
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

    @staticmethod
    def _pearson(a: list[float], b: list[float]) -> float:
        n = len(a)
        if n == 0:
            return 0.0
        mean_a = sum(a) / n
        mean_b = sum(b) / n
        cov = sum((x - mean_a) * (y - mean_b) for x, y in zip(a, b))
        var_a = sum((x - mean_a) ** 2 for x in a)
        var_b = sum((y - mean_b) ** 2 for y in b)
        denom = (var_a * var_b) ** 0.5
        if denom == 0:
            return 0.0
        return cov / denom
