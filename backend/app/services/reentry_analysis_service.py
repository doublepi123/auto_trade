"""Re-entry behavior analysis service.

Conditions each same-symbol round trip on the previous trade's outcome
to detect behavioral tilt: does the system trade better right after a
win or right after a loss?  Read-only.

Inspired by Freqtrade's sequential trade-pair analysis and tilt detection
in trading journals.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from app.services.analytics_trade_sample_service import (
    analytics_response,
    load_analytics_trade_sample,
    mixed_currency_error,
)
from app.services.daily_pnl_service import ClosedRoundTrip

__all__ = ["ReentryAnalysisService"]


class _Bucket:
    __slots__ = ("n", "wins", "pnl")

    def __init__(self) -> None:
        self.n = 0
        self.wins = 0
        self.pnl = 0.0

    def add(self, pnl: float) -> None:
        self.n += 1
        if pnl > 0:
            self.wins += 1
        self.pnl += pnl

    def as_dict(self) -> dict[str, Any]:
        return {
            "trades": self.n,
            "win_rate": round(self.wins / self.n, 4) if self.n else None,
            "avg_pnl": round(self.pnl / self.n, 2) if self.n else None,
            "total_pnl": round(self.pnl, 2),
        }


@dataclass(frozen=True)
class _EntryOutcome:
    symbol: str
    entry_order_id: int
    entry_at: datetime
    exit_at: datetime
    exit_order_id: int
    net_pnl: float


class ReentryAnalysisService:
    """Conditional outcome analytics for same-symbol re-entries."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def summary(self, days: int = 90) -> dict[str, Any]:
        sample = load_analytics_trade_sample(
            self._db,
            lookback_days=days,
        )
        rows = _entry_outcomes(sample.trades)
        currency_error = mixed_currency_error(
            sample,
            payload={"days": days, "sample_size": len(rows)},
        )
        if currency_error is not None:
            return currency_error
        if len(rows) < 6:
            return analytics_response(
                sample,
                {
                    "days": days,
                    "sample_size": len(rows),
                    "error": "Need at least 6 independent entries.",
                },
            )

        after_win = _Bucket()
        after_loss = _Bucket()
        after_scratch = _Bucket()
        first_of_symbol = _Bucket()
        overlapping_entry = _Bucket()
        by_symbol: dict[str, dict[str, _Bucket]] = defaultdict(
            lambda: {"after_win": _Bucket(), "after_loss": _Bucket()}
        )

        episodes_by_symbol: dict[str, list[_EntryOutcome]] = defaultdict(list)
        for row in rows:
            episodes_by_symbol[row.symbol].append(row)

        for row in rows:
            earlier_entries = [
                candidate
                for candidate in episodes_by_symbol[row.symbol]
                if (
                    candidate.entry_at,
                    candidate.entry_order_id,
                    candidate.exit_order_id,
                )
                < (row.entry_at, row.entry_order_id, row.exit_order_id)
            ]
            causal_predecessors = [
                candidate
                for candidate in earlier_entries
                if candidate.exit_at <= row.entry_at
            ]
            if not causal_predecessors:
                if earlier_entries:
                    overlapping_entry.add(row.net_pnl)
                else:
                    first_of_symbol.add(row.net_pnl)
                continue
            previous = max(
                causal_predecessors,
                key=lambda candidate: (
                    candidate.exit_at,
                    candidate.exit_order_id,
                    candidate.entry_order_id,
                ),
            )
            if previous.net_pnl > 0:
                after_win.add(row.net_pnl)
                by_symbol[row.symbol]["after_win"].add(row.net_pnl)
            elif previous.net_pnl < 0:
                after_loss.add(row.net_pnl)
                by_symbol[row.symbol]["after_loss"].add(row.net_pnl)
            else:
                after_scratch.add(row.net_pnl)

        symbol_rows = [
            {
                "symbol": sym,
                "after_win": buckets["after_win"].as_dict(),
                "after_loss": buckets["after_loss"].as_dict(),
            }
            for sym, buckets in sorted(
                by_symbol.items(),
                key=lambda item: (
                    -(
                        item[1]["after_win"].n
                        + item[1]["after_loss"].n
                    ),
                    item[0],
                ),
            )
        ]

        tilt = None
        if after_win.n >= 3 and after_loss.n >= 3:
            aw = after_win.pnl / after_win.n
            al = after_loss.pnl / after_loss.n
            tilt = round(aw - al, 2)

        return analytics_response(
            sample,
            {
                "days": days,
                "sample_size": len(rows),
                "after_win": after_win.as_dict(),
                "after_loss": after_loss.as_dict(),
                "after_scratch": after_scratch.as_dict(),
                "first_of_symbol": first_of_symbol.as_dict(),
                "overlapping_entry": overlapping_entry.as_dict(),
                "tilt_avg_pnl_diff": tilt,
                "by_symbol": symbol_rows,
            },
        )


def _entry_outcomes(trades: list[ClosedRoundTrip]) -> list[_EntryOutcome]:
    grouped: dict[tuple[str, int], list[ClosedRoundTrip]] = defaultdict(list)
    for trade in trades:
        # Synthetic external entries use id=0 and cannot safely be merged with
        # one another. Their exit id provides a stable independent fallback.
        entry_identity = (
            trade.entry_order_id
            if trade.entry_order_id > 0
            else -trade.exit_order_id
        )
        grouped[(trade.symbol, entry_identity)].append(trade)

    outcomes = [
        _EntryOutcome(
            symbol=slices[0].symbol,
            entry_order_id=entry_identity,
            entry_at=min(item.entry_at for item in slices),
            exit_at=max(item.exit_at for item in slices),
            exit_order_id=max(item.exit_order_id for item in slices),
            net_pnl=sum(item.net_pnl for item in slices),
        )
        for (_symbol, entry_identity), slices in grouped.items()
    ]
    return sorted(
        outcomes,
        key=lambda row: (
            row.entry_at,
            row.entry_order_id,
            row.exit_at,
            row.exit_order_id,
            row.symbol,
        ),
    )
