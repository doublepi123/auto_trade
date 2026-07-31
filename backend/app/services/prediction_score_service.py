"""Retrospective conditional outcome-frequency service.

Summarizes historical win rates conditional on entry-time features
(day-of-week, hour, and the latest outcome known before entry). It does not
produce a per-trade forecast or a live decision signal. Read-only.

Inspired by Freqtrade's hyperopt feature importance and Edgewonk's
trade grading system.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    market_local_datetime,
)

__all__ = ["PredictionScoreService"]

_EVIDENCE_MODE = "RETROSPECTIVE_CONDITIONAL_FREQUENCY"


class PredictionScoreService:
    """Retrospective conditional win rates by entry-observable features."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def analyze(
        self, symbol: str | None = None, lookback_days: int = 180
    ) -> dict[str, Any]:
        normalized_symbol = (
            symbol.strip().upper() if symbol and symbol.strip() else None
        )
        sample = load_analytics_trade_sample(
            self._db,
            symbol=normalized_symbol,
            lookback_days=lookback_days,
        )
        trades = sorted(
            sample.trades,
            key=lambda trade: (
                trade.entry_at,
                trade.entry_order_id,
                trade.exit_at,
                trade.exit_order_id,
            ),
        )
        if len(trades) < 20:
            return analytics_response(
                sample,
                {
                    "symbol": normalized_symbol or "ALL",
                    "lookback_days": lookback_days,
                    "sample_size": len(trades),
                    "evidence_mode": _EVIDENCE_MODE,
                    "live_decision_allowed": False,
                    "error": "Need at least 20 closed trades.",
                },
            )

        # Entry weekday/hour are observable before the outcome.  The streak
        # label uses only trades that had closed strictly before this entry, so
        # overlapping positions and same-timestamp fills cannot leak a future
        # result into the feature set.
        by_dow: dict[int, list[bool]] = defaultdict(list)
        by_hour: dict[int, list[bool]] = defaultdict(list)
        by_streak: dict[str, list[bool]] = defaultdict(list)
        closed = sorted(
            trades,
            key=lambda trade: (trade.exit_at, trade.exit_order_id),
        )
        known_outcomes: list[float] = []
        closed_index = 0
        for trade in trades:
            while (
                closed_index < len(closed)
                and closed[closed_index].exit_at < trade.entry_at
            ):
                known_outcomes.append(closed[closed_index].net_pnl)
                closed_index += 1
            local_entry = market_local_datetime(trade.symbol, trade.entry_at)
            win = trade.net_pnl > 0
            by_dow[local_entry.weekday()].append(win)
            by_hour[local_entry.hour].append(win)
            prior = known_outcomes[-1] if known_outcomes else None
            streak_label = (
                "after_win"
                if prior is not None and prior > 0
                else "after_loss"
                if prior is not None and prior < 0
                else "neutral"
            )
            by_streak[streak_label].append(win)

        def _wr(outcomes: list[bool]) -> float:
            return sum(outcomes) / len(outcomes) if outcomes else 0.5

        dow_wr = {d: round(_wr(v), 4) for d, v in sorted(by_dow.items())}
        hour_wr = {h: round(_wr(v), 4) for h, v in sorted(by_hour.items())}
        streak_wr = {
            key: round(_wr(by_streak[key]), 4)
            for key in ("after_win", "after_loss", "neutral")
            if by_streak[key]
        }

        # overall baseline
        all_wins = [trade.net_pnl > 0 for trade in trades]
        baseline_wr = sum(all_wins) / len(all_wins)

        # best/worst conditional edges
        all_features: list[tuple[str, float, int]] = []
        for d, wr in dow_wr.items():
            if len(by_dow[d]) >= 3:
                all_features.append((f"dow_{d}", wr, len(by_dow[d])))
        for h, wr in hour_wr.items():
            if len(by_hour[h]) >= 3:
                all_features.append((f"hour_{h}", wr, len(by_hour[h])))

        top_edges = sorted(all_features, key=lambda item: (-item[1], item[0]))[:5]
        bottom_edges = sorted(all_features, key=lambda item: (item[1], item[0]))[:5]

        return analytics_response(
            sample,
            {
                "symbol": normalized_symbol or "ALL",
                "lookback_days": lookback_days,
                "sample_size": len(trades),
                "evidence_mode": _EVIDENCE_MODE,
                "live_decision_allowed": False,
                "baseline_win_rate": round(baseline_wr, 4),
                "dow_win_rates": dow_wr,
                "hour_win_rates": hour_wr,
                "streak_win_rates": streak_wr,
                "top_edges": [
                    {"feature": feature, "win_rate": wr, "count": count}
                    for feature, wr, count in top_edges
                ],
                "bottom_edges": [
                    {"feature": feature, "win_rate": wr, "count": count}
                    for feature, wr, count in bottom_edges
                ],
                "edge_spread": (
                    round(top_edges[0][1] - bottom_edges[0][1], 4)
                    if top_edges and bottom_edges
                    else 0
                ),
            },
        )
