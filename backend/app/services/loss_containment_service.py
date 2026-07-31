"""Loss containment analysis service.

Examines losing trades: how losses distribute by exit cause, whether a
small set of tail losses dominates total damage, and how losses compare
against the median loss (tail-breach ratio).  Read-only.

Inspired by VectorBT's stop-loss analysis and Freqtrade's stoploss guard
evaluation.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from statistics import median
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["LossContainmentService"]

_BUCKETS: list[tuple[str, float, float]] = [
    ("0~0.5x", 0.0, 0.5),
    ("0.5x~1x", 0.5, 1.0),
    ("1x~2x", 1.0, 2.0),
    ("2x~3x", 2.0, 3.0),
    (">3x", 3.0, float("inf")),
]


class LossContainmentService:
    """Losing-trade distribution and tail-breach analytics."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        rows = self._fetch(days)
        if len(rows) < 3:
            return {
                "days": days,
                "sample_size": len(rows),
                "error": "Need at least 3 losing trades.",
            }

        magnitudes = [-p for _, p in rows]
        total_loss = sum(magnitudes)
        med = median(magnitudes)
        mean = total_loss / len(magnitudes)
        worst = max(magnitudes)

        sorted_desc = sorted(magnitudes, reverse=True)
        top3_share = sum(sorted_desc[:3]) / total_loss if total_loss > 0 else None

        tail_threshold = 2 * med
        tail_count = sum(1 for m in magnitudes if m > tail_threshold)

        by_cause: dict[str, dict[str, float]] = defaultdict(lambda: {"n": 0.0, "loss": 0.0})
        for cause, pnl in rows:
            bucket = by_cause[cause or "UNKNOWN"]
            bucket["n"] += 1
            bucket["loss"] += -pnl

        cause_rows = [
            {
                "exit_cause": cause,
                "count": int(v["n"]),
                "total_loss": round(v["loss"], 2),
                "avg_loss": round(v["loss"] / v["n"], 2),
                "share_of_loss": round(v["loss"] / total_loss, 4) if total_loss > 0 else None,
            }
            for cause, v in sorted(by_cause.items(), key=lambda kv: kv[1]["loss"], reverse=True)
        ]

        counts = [0] * len(_BUCKETS)
        for m in magnitudes:
            ratio = m / med if med > 0 else 0.0
            for i, (_label, lo, hi) in enumerate(_BUCKETS):
                if lo <= ratio < hi:
                    counts[i] += 1
                    break

        histogram = [
            {"bucket": label, "count": counts[i]} for i, (label, _lo, _hi) in enumerate(_BUCKETS)
        ]

        return {
            "days": days,
            "sample_size": len(rows),
            "total_loss": round(total_loss, 2),
            "median_loss": round(med, 2),
            "mean_loss": round(mean, 2),
            "worst_loss": round(worst, 2),
            "worst_to_median": round(worst / med, 2) if med > 0 else None,
            "top3_loss_share": round(top3_share, 4) if top3_share is not None else None,
            "tail_breach_count": tail_count,
            "tail_breach_pct": round(tail_count / len(magnitudes), 4),
            "by_exit_cause": cause_rows,
            "histogram": histogram,
        }

    def _fetch(self, days: int) -> list[tuple[str, float]]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = (
            select(OrderRecord.exit_cause, OrderRecord.net_pnl)
            .where(
                OrderRecord.net_pnl.is_not(None),
                OrderRecord.net_pnl < 0,
                OrderRecord.filled_at >= cutoff,
            )
            .order_by(OrderRecord.filled_at.asc())
        )
        return [
            (r[0] or "UNKNOWN", float(r[1])) for r in self._db.execute(stmt).all() if r[1] is not None
        ]
