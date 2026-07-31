"""Scratch (breakeven) trade analysis service.

Identifies trades whose net outcome barely covered their costs
(|net_pnl| <= estimated round-trip fees) and measures their frequency,
holding time, and opportunity cost versus decisive trades.  Read-only.

Inspired by Edgewonk's scratch-trade journaling metrics.
"""
from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
    trade_local_day,
)
from app.services.daily_pnl_service import ClosedRoundTrip

__all__ = ["ScratchAnalysisService"]


class ScratchAnalysisService:
    """Scratch-trade rate and cost analytics over closed trades."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
        )
        rows = sample.trades
        currency_error = mixed_currency_error(
            sample,
            payload={"days": days, "sample_size": len(rows)},
        )
        if currency_error is not None:
            return currency_error
        if len(rows) < 5:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(rows),
                    "error": "Need at least 5 closed trades.",
                },
            )

        scratch_holds: list[float] = []
        decisive_holds: list[float] = []
        scratch_by_symbol: dict[str, dict[str, int]] = defaultdict(lambda: {"scratch": 0, "total": 0})
        by_week: dict[str, dict[str, int]] = defaultdict(lambda: {"scratch": 0, "total": 0})
        scratch_count = 0
        scratch_fee_total = 0.0

        for row in rows:
            net = row.net_pnl
            fee = row.est_fees
            band = max(fee, 1e-9)
            is_scratch = abs(net) <= band

            hold_min = _holding_minutes(row)
            if is_scratch:
                scratch_count += 1
                scratch_fee_total += fee
                if hold_min is not None:
                    scratch_holds.append(hold_min)
            elif hold_min is not None:
                decisive_holds.append(hold_min)

            sym = scratch_by_symbol[row.symbol]
            sym["total"] += 1
            if is_scratch:
                sym["scratch"] += 1

            week = trade_local_day(row.symbol, row.exit_at).isocalendar()
            key = f"{week[0]}-W{week[1]:02d}"
            by_week[key]["total"] += 1
            if is_scratch:
                by_week[key]["scratch"] += 1

        n = len(rows)
        symbol_rows = [
            {
                "symbol": sym,
                "total": v["total"],
                "scratch": v["scratch"],
                "scratch_rate": round(v["scratch"] / v["total"], 4),
            }
            for sym, v in sorted(
                scratch_by_symbol.items(),
                key=lambda item: (
                    -(item[1]["scratch"] / item[1]["total"]),
                    item[0],
                ),
            )
        ]

        weekly = [
            {
                "week": w,
                "total": v["total"],
                "scratch_rate": round(v["scratch"] / v["total"], 4),
            }
            for w, v in sorted(by_week.items())
        ]

        return analytics_response(
            sample,
            {
                "days": days,
                "sample_size": n,
                "scratch_count": scratch_count,
                "scratch_rate": round(scratch_count / n, 4),
                "scratch_fee_total": round(scratch_fee_total, 2),
                "avg_scratch_hold_min": (
                    round(sum(scratch_holds) / len(scratch_holds), 1)
                    if scratch_holds
                    else None
                ),
                "avg_decisive_hold_min": (
                    round(sum(decisive_holds) / len(decisive_holds), 1)
                    if decisive_holds
                    else None
                ),
                "median_scratch_hold_min": (
                    round(median(scratch_holds), 1)
                    if scratch_holds
                    else None
                ),
                "by_symbol": symbol_rows,
                "weekly": weekly,
            },
        )


def _holding_minutes(row: ClosedRoundTrip) -> float | None:
    minutes = row.holding_seconds / 60.0
    return minutes if minutes >= 0 else None
