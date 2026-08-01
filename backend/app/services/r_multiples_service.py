"""Post-hoc realized-loss multiple distribution service.

Normalizes each closed trade's net PnL by the sample's mean realized loss and
reports the resulting distribution. This is a retrospective proxy, not the
initial risk frozen at entry. Read-only.

Inspired by Edgewonk's R-multiple journaling and QuantStats' risk-normalized
return distribution.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["RMultiplesService"]

_RISK_UNIT_METHOD = "MEAN_REALIZED_LOSS_PROXY"

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
    """Trade outcomes normalized by a mean realized-loss proxy."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def distribution(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
        )
        pnls = [trade.net_pnl for trade in sample.trades]
        currency_error = mixed_currency_error(
            sample,
            payload={
                "days": days,
                "sample_size": len(pnls),
                "risk_unit_method": _RISK_UNIT_METHOD,
                "true_initial_risk_available": False,
            },
        )
        if currency_error is not None:
            return currency_error
        if len(pnls) < 5:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(pnls),
                    "risk_unit_method": _RISK_UNIT_METHOD,
                    "true_initial_risk_available": False,
                    "error": "Need at least 5 closed trades.",
                },
            )

        losses = [p for p in pnls if p < 0]
        if not losses:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(pnls),
                    "risk_unit_method": _RISK_UNIT_METHOD,
                    "true_initial_risk_available": False,
                    "error": (
                        "No losing trades in window; cannot derive 1R risk "
                        "unit."
                    ),
                },
            )

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

        return analytics_response(
            sample,
            {
                "days": days,
                "sample_size": n,
                "risk_unit_method": _RISK_UNIT_METHOD,
                "true_initial_risk_available": False,
                "risk_unit": round(risk_unit, 2),
                "expectancy_r": round(expectancy_r, 4),
                "pct_ge_1r": round(big_win, 4),
                "pct_le_minus_1r": round(big_loss, 4),
                "min_r": round(min(multiples), 2),
                "max_r": round(max(multiples), 2),
                "histogram": histogram,
            },
        )
