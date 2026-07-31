"""R-multiple distribution service.

Normalizes each closed trade's net PnL by the system's average loss
(one "R" of risk) and reports the resulting distribution: histogram
buckets, expectancy in R, and tail shares.  Read-only.

Inspired by Edgewonk's R-multiple journaling and QuantStats' risk-normalized
return distribution.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RMultiplesService"]

_BUCKETS: list[tuple[str, float, float]] = [
    ("<-3R", float("-inf"), -3.0),
    ("-3R~-2R", -3.0, -2.0),
    ("-2R~-1R", -2.0, -1.0),
    ("-1R~-0.5R", -1.0, -0.5),
    ("-0.5R~0", -0.5, 0.0),
    ("0~0.5R", 0.0, 0.5),
    ("0.5R~1R", 0.5, 1.0),
    ("1R~2R", 1.0, 2.0),
    ("2R~3R", 2.0, 3.0),
    (">3R", 3.0, float("inf")),
]


class RMultiplesService:
    """Risk-normalized (R-multiple) trade outcome distribution."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def distribution(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 5:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 5 closed trades.",
            }

        pnls = [float(r[0]) for r in rows]
        losses = [p for p in pnls if p < 0]
        if not losses:
            return {
                "days": days,
                "sample_size": len(pnls),
                "error": "No losing trades in window; cannot derive 1R risk unit.",
            }

        risk_unit = sum(-p for p in losses) / len(losses)
        multiples = [p / risk_unit for p in pnls]

        counts = [0] * len(_BUCKETS)
        for r in multiples:
            for i, (_label, lo, hi) in enumerate(_BUCKETS):
                if lo <= r < hi:
                    counts[i] += 1
                    break

        n = len(multiples)
        expectancy_r = sum(multiples) / n
        big_win = sum(1 for r in multiples if r >= 1.0) / n
        big_loss = sum(1 for r in multiples if r <= -1.0) / n

        histogram = [
            {
                "bucket": label,
                "count": counts[i],
                "share": round(counts[i] / n, 4),
            }
            for i, (label, _lo, _hi) in enumerate(_BUCKETS)
        ]

        return {
            "days": days,
            "sample_size": n,
            "risk_unit": round(risk_unit, 2),
            "expectancy_r": round(expectancy_r, 4),
            "pct_ge_1r": round(big_win, 4),
            "pct_le_minus_1r": round(big_loss, 4),
            "min_r": round(min(multiples), 2),
            "max_r": round(max(multiples), 2),
            "histogram": histogram,
        }

    def _fetch(self, days: int) -> list[tuple[float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.net_pnl)
            .where(OrderRecord.net_pnl.is_not(None), OrderRecord.filled_at >= cutoff)
            .order_by(OrderRecord.filled_at.asc())
        )
        return [(float(r[0]),) for r in self._db.execute(stmt).all() if r[0] is not None]
