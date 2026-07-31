"""PnL milestone tracker service.

Tracks how many trades it took to reach cumulative PnL milestones and
detects acceleration or deceleration in milestone pace.  Read-only.

Inspired by Edgewonk's progress tracking and QuantStats' cumulative
return analytics.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)

__all__ = ["MilestoneService"]


class MilestoneService:
    """Cumulative PnL milestone pace tracking."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def track(
        self,
        symbol: str | None = None,
        lookback_days: int = 365,
        step: float = 100.0,
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
        if len(pnls) < 5:
            return analytics_response(
                sample,
                {
                    "symbol": symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(pnls),
                    "error": "Need at least 5 closed trades.",
                },
            )

        # compute cumulative PnL and find milestone crossings
        cum = 0.0
        milestones: list[dict[str, Any]] = []
        next_up = step
        next_down = -step

        for i, pnl in enumerate(pnls):
            cum += pnl
            while cum >= next_up:
                milestones.append(
                    {"level": round(next_up, 2), "trade_index": i, "direction": "up"}
                )
                next_up += step
            while cum <= next_down:
                milestones.append(
                    {"level": round(next_down, 2), "trade_index": i, "direction": "down"}
                )
                next_down -= step

        # compute pace between consecutive same-direction milestones
        up_milestones = [m for m in milestones if m["direction"] == "up"]
        down_milestones = [m for m in milestones if m["direction"] == "down"]

        def _paces(ms: list[dict[str, Any]]) -> list[int]:
            return [
                ms[i]["trade_index"] - ms[i - 1]["trade_index"]
                for i in range(1, len(ms))
            ]

        up_paces = _paces(up_milestones)
        down_paces = _paces(down_milestones)

        # acceleration: compare first half vs second half pace
        def _accel(paces: list[int]) -> str:
            if len(paces) < 4:
                return "insufficient"
            first = sum(paces[: len(paces) // 2]) / (len(paces) // 2)
            second = sum(paces[len(paces) // 2 :]) / (len(paces) - len(paces) // 2)
            if second < first * 0.8:
                return "accelerating"
            if second > first * 1.2:
                return "decelerating"
            return "stable"

        return analytics_response(
            sample,
            {
                "symbol": symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(pnls),
                "step": step,
                "final_cumulative_pnl": round(cum, 2),
                "total_milestones": len(milestones),
                "up_milestones": len(up_milestones),
                "down_milestones": len(down_milestones),
                "milestones": milestones[-20:],
                "pace": {
                    "avg_up_pace": round(sum(up_paces) / len(up_paces), 1) if up_paces else None,
                    "avg_down_pace": round(sum(down_paces) / len(down_paces), 1) if down_paces else None,
                    "up_acceleration": _accel(up_paces),
                    "down_acceleration": _accel(down_paces),
                },
            },
        )
