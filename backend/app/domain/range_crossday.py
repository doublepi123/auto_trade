"""An isolated, record-only cross-day loss hypothesis. Never submits orders."""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from app.core.market_calendar import next_session_open, trade_day_for
from app.domain.range_replay import RangeReplayConfig, RangeReplayEngine, TapeQuote


class RangeCrossdayEngine(RangeReplayEngine):
    def __init__(self, config: RangeReplayConfig, *, candidate: bool) -> None:
        super().__init__(config, "crossing_baseline")
        if type(candidate) is not bool:
            raise ValueError("candidate must be a boolean")
        self.candidate = candidate
        self.crossday_losses = 0
        self.rest_through: date | None = None
        self.rest_activations = 0

    def _roll_clocks(self, q: TapeQuote) -> None:
        super()._roll_clocks(q)
        if self.rest_through is not None and trade_day_for("US", q.observed_at) > self.rest_through:
            self.rest_through = None
            self.crossday_losses = 0

    def _entry_allowed(self, q: TapeQuote) -> bool:
        if self.candidate and self.rest_through is not None:
            return False
        return super()._entry_allowed(q)

    def _try_exit_fill(self, q: TapeQuote) -> None:
        previous = len(self.closed)
        super()._try_exit_fill(q)
        if len(self.closed) == previous:
            return
        net = Decimal(self.closed[-1]["costs"]["confirmation"]["net"])
        self.crossday_losses = self.crossday_losses+1 if net < 0 else 0
        if self.candidate and self.crossday_losses >= 5:
            self.rest_through = trade_day_for("US", next_session_open("US", q.observed_at))
            self.rest_activations += 1

    def report(self) -> dict[str, Any]:
        return {
            **super().report(), "algorithm_version": "range-crossday-loss-v1",
            "arm": "crossday_candidate" if self.candidate else "crossday_baseline",
            "crossday_loss_streak": self.crossday_losses,
            "crossday_rest_through": self.rest_through.isoformat() if self.rest_through else None,
            "crossday_rest_activations": self.rest_activations,
        }
