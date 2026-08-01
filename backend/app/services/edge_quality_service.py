"""Edge quality scoring service.

Produces a single composite "edge quality" score from multiple statistical
tests on the trade history: consistency, expectancy stability, drawdown
control, and sample adequacy.  Read-only.

Inspired by Edgewonk's edge-score and QuantStats' strategy quality metrics.
"""
from __future__ import annotations

import math
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["EdgeQualityService"]


class EdgeQualityService:
    """Composite edge quality scoring."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def score(
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
        pnls = [trade.net_pnl for trade in sample.trades]
        if len(pnls) < 10:
            return analytics_response(sample, {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "error": "Need at least 10 closed trades.",
            })

        n = len(pnls)
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        win_rate = len(wins) / n
        avg_win = sum(wins) / len(wins) if wins else 0
        avg_loss = abs(sum(losses) / len(losses)) if losses else 1
        expectancy = sum(pnls) / n

        # Factor 1: Expectancy positivity (0-25 pts)
        expectancy_score = min(max(expectancy / max(avg_loss, 0.01), 0), 1.0) * 25

        # Factor 2: Consistency — rolling win-rate stability (0-25 pts)
        window = min(20, n // 2)
        if window >= 5:
            rolling_wrs = []
            for i in range(window, n + 1):
                w = pnls[i - window : i]
                rolling_wrs.append(sum(1 for p in w if p > 0) / window)
            wr_std = _std(rolling_wrs)
            consistency_score = max(0, 25 - wr_std * 100)
        else:
            consistency_score = 12.5

        # Factor 3: Drawdown control (0-25 pts)
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            peak = max(peak, cum)
            max_dd = max(max_dd, peak - cum)
        total_profit = sum(wins) if wins else 1
        dd_ratio = max_dd / max(total_profit, 1)
        dd_score = max(0, 25 * (1 - min(dd_ratio, 1.0)))

        # Factor 4: Sample adequacy (0-25 pts)
        sample_score = min(n / 100.0, 1.0) * 25

        composite = expectancy_score + consistency_score + dd_score + sample_score
        if expectancy <= 0:
            # Stable losses and a large sample are not evidence of an edge.
            composite = min(composite, 34.99)
        grade = _grade(composite)

        return analytics_response(sample, {
            "symbol": symbol or "ALL",
            "lookback_days": lookback_days,
            "sample_size": n,
            "composite_score": round(composite, 2),
            "grade": grade,
            "factors": {
                "expectancy": {"score": round(expectancy_score, 2), "max": 25, "detail": f"expectancy={expectancy:.2f}, avg_loss={avg_loss:.2f}"},
                "consistency": {"score": round(consistency_score, 2), "max": 25, "detail": f"rolling WR std={_std(rolling_wrs):.4f}" if window >= 5 else "insufficient window"},
                "drawdown_control": {"score": round(dd_score, 2), "max": 25, "detail": f"max_dd/profit={dd_ratio:.3f}"},
                "sample_adequacy": {"score": round(sample_score, 2), "max": 25, "detail": f"n={n}"},
            },
            "underlying": {
                "win_rate": round(win_rate, 4),
                "expectancy": round(expectancy, 2),
                "payoff_ratio": round(avg_win / avg_loss, 4) if avg_loss > 0 else None,
                "max_drawdown": round(max_dd, 2),
            },
            "recommendation": _recommend(grade, composite),
        })


def _std(vals: list[float]) -> float:
    if not vals:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _grade(score: float) -> str:
    if score >= 80:
        return "A"
    if score >= 65:
        return "B"
    if score >= 50:
        return "C"
    if score >= 35:
        return "D"
    return "F"


def _recommend(grade: str, score: float) -> str:
    if grade == "A":
        return "Strong edge — maintain current parameters and monitor for decay."
    if grade == "B":
        return "Good edge — minor improvements in consistency or drawdown control could push to A."
    if grade == "C":
        return "Marginal edge — review entry/exit logic and risk management."
    if grade == "D":
        return "Weak edge — significant rework needed before scaling up."
    return "No detectable edge — consider pausing live trading and revisiting strategy design."
