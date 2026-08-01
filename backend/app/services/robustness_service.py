"""Strategy robustness index service.

Produces a composite robustness score by testing how sensitive the
strategy edge is to perturbations: sub-period stability, outlier
dependence, and parameter-free consistency.  Read-only.

Inspired by QuantStats' strategy quality metrics and VectorBT's
robustness testing.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["RobustnessService"]


class RobustnessService:
    """Composite robustness scoring via perturbation sensitivity."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def score(
        self, symbol: str | None = None, lookback_days: int = 365
    ) -> dict[str, Any]:
        normalized_symbol = (
            symbol.strip().upper() if symbol and symbol.strip() else None
        )
        sample = load_analytics_trade_sample(
            self._db,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        pnls = [trade.net_pnl for trade in sample.trades]
        currency_error = mixed_currency_error(
            sample,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        if currency_error is not None:
            return currency_error
        if len(pnls) < 20:
            return analytics_response(
                sample,
                {
                    "symbol": normalized_symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(pnls),
                    "error": "Need at least 20 closed trades.",
                },
            )

        n = len(pnls)
        total_pnl = sum(pnls)
        wins = [p for p in pnls if p > 0]

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
        outlier_score = (
            30.0
            if pnl_without_top3 > 0
            else 15.0
            if pnl_without_top3 > -top3_wins * 0.5
            else 0.0
        )

        # Factor 3: Win-rate consistency across halves
        first_half = pnls[: n // 2]
        second_half = pnls[n // 2 :]
        wr1 = sum(1 for p in first_half if p > 0) / len(first_half) if first_half else 0
        wr2 = sum(1 for p in second_half if p > 0) / len(second_half) if second_half else 0
        wr_diff = abs(wr1 - wr2)
        consistency_score = max(0, 20 - wr_diff * 50)  # max 20 pts

        # Factor 4: Sample adequacy (max 20 pts)
        sample_score = min(n / 200.0, 1.0) * 20

        composite = (
            stability_score
            + outlier_score
            + consistency_score
            + sample_score
        )
        # Stability of a losing process is not evidence of a durable edge.
        # Fail closed instead of awarding B/C/D solely for consistency/sample.
        if total_pnl <= 0:
            composite = min(composite, 29.99)
            grade = "F"
        else:
            grade = _grade(composite)

        return analytics_response(
            sample,
            {
                "symbol": normalized_symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": n,
                "total_pnl": round(total_pnl, 2),
                "composite_score": round(composite, 2),
                "grade": grade,
                "factors": {
                    "sub_period_stability": {
                        "score": round(stability_score, 2),
                        "max": 30,
                        "detail": f"{positive_quarters}/4 quarters positive",
                    },
                    "outlier_independence": {
                        "score": round(outlier_score, 2),
                        "max": 30,
                        "detail": (
                            f"PnL w/o top3 wins: {pnl_without_top3:.2f}"
                        ),
                    },
                    "wr_consistency": {
                        "score": round(consistency_score, 2),
                        "max": 20,
                        "detail": (
                            f"WR diff: {wr_diff:.3f} "
                            f"({wr1:.3f} vs {wr2:.3f})"
                        ),
                    },
                    "sample_adequacy": {
                        "score": round(sample_score, 2),
                        "max": 20,
                        "detail": f"n={n}",
                    },
                },
                "quarter_pnls": [round(qp, 2) for qp in quarter_pnls],
                "recommendation": _recommend(grade, total_pnl=total_pnl),
            },
        )


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


def _recommend(grade: str, *, total_pnl: float) -> str:
    if total_pnl <= 0:
        return (
            "Not robust — aggregate net PnL is non-positive, so consistency "
            "cannot be treated as a reliable strategy edge."
        )
    if grade == "A":
        return "Highly robust — edge persists across sub-periods and is not outlier-dependent."
    if grade == "B":
        return "Reasonably robust — minor sensitivity to outliers or sub-period variation."
    if grade == "C":
        return "Moderate robustness — edge may be fragile under regime changes."
    if grade == "D":
        return "Low robustness — edge heavily depends on few trades or specific periods."
    return "Not robust — strategy edge is not statistically reliable."
