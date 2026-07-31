"""Strategy robustness index service.

Produces a composite robustness score by testing how sensitive the
strategy edge is to perturbations: sub-period stability, outlier
dependence, and parameter-free consistency.  Read-only.

Inspired by QuantStats' strategy quality metrics and VectorBT's
robustness testing.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import OrderRecord

__all__ = ["RobustnessService"]


class RobustnessService:
    """Composite robustness scoring via perturbation sensitivity."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def score(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        pnls = self._fetch_pnls(symbol, lookback_days)
        if len(pnls) < 20:
            return {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 20 closed trades.",
            }

        n = len(pnls)
        total_pnl = sum(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        # Factor 1: Sub-period stability (split into 4 quarters)
        q = n // 4
        quarters = [pnls[i * q : (i + 1) * q] for i in range(3)]
        quarters.append(pnls[3 * q :])
        quarter_pnls = [sum(qtr) for qtr in quarters]
        positive_quarters = sum(1 for qp in quarter_pnls if qp > 0)
        stability_score = positive_quarters / 4.0 * 30  # max 30 pts

        # Factor 2: Outlier dependence — remove top 3 wins, is edge still positive?
        sorted_wins = sorted(wins, reverse=True)
        top3_wins = sum(sorted_wins[:3]) if len(sorted_wins) >= 3 else sum(sorted_wins)
        pnl_without_top3 = total_pnl - top3_wins
        outlier_score = 30.0 if pnl_without_top3 > 0 else (15.0 if pnl_without_top3 > -top3_wins * 0.5 else 0.0)

        # Factor 3: Win-rate consistency across halves
        first_half = pnls[: n // 2]
        second_half = pnls[n // 2 :]
        wr1 = sum(1 for p in first_half if p > 0) / len(first_half) if first_half else 0
        wr2 = sum(1 for p in second_half if p > 0) / len(second_half) if second_half else 0
        wr_diff = abs(wr1 - wr2)
        consistency_score = max(0, 20 - wr_diff * 50)  # max 20 pts

        # Factor 4: Sample adequacy (max 20 pts)
        sample_score = min(n / 200.0, 1.0) * 20

        composite = stability_score + outlier_score + consistency_score + sample_score
        grade = _grade(composite)

        return {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "composite_score": round(composite, 2),
            "grade": grade,
            "factors": {
                "sub_period_stability": {"score": round(stability_score, 2), "max": 30, "detail": f"{positive_quarters}/4 quarters positive"},
                "outlier_independence": {"score": round(outlier_score, 2), "max": 30, "detail": f"PnL w/o top3 wins: {pnl_without_top3:.2f}"},
                "wr_consistency": {"score": round(consistency_score, 2), "max": 20, "detail": f"WR diff: {wr_diff:.3f} ({wr1:.3f} vs {wr2:.3f})"},
                "sample_adequacy": {"score": round(sample_score, 2), "max": 20, "detail": f"n={n}"},
            },
            "quarter_pnls": [round(qp, 2) for qp in quarter_pnls],
            "recommendation": _recommend(grade),
        }

    def _fetch_pnls(self, symbol: str | None, days: int) -> list[float]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        stmt = select(OrderRecord.net_pnl).where(
            OrderRecord.net_pnl.is_not(None),
            OrderRecord.filled_at >= cutoff,
        )
        if symbol:
            stmt = stmt.where(OrderRecord.symbol == symbol)
        stmt = stmt.order_by(OrderRecord.filled_at.asc())
        rows = self._db.scalars(stmt).all()
        return [float(r) for r in rows if r is not None]


def _grade(score: float) -> str:
    if score >= 75:
        return "A"
    if score >= 60:
        return "B"
    if score >= 45:
        return "C"
    if score >= 30:
        return "D"
    return "F"


def _recommend(grade: str) -> str:
    if grade == "A":
        return "Highly robust — edge persists across sub-periods and is not outlier-dependent."
    if grade == "B":
        return "Reasonably robust — minor sensitivity to outliers or sub-period variation."
    if grade == "C":
        return "Moderate robustness — edge may be fragile under regime changes."
    if grade == "D":
        return "Low robustness — edge heavily depends on few trades or specific periods."
    return "Not robust — strategy edge is not statistically reliable."
