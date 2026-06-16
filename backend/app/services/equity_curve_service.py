"""Account-wide cumulative realized PnL curve (equity curve) over closed trips.

Buckets ``ClosedRoundTrip`` rows by exit day and folds them into a cumulative
realized-PnL time series with a running peak-to-trough drawdown — the
always-on, account-wide (all symbols) view that the per-symbol Reports chart
and the intraday Dashboard PnLChart do not provide. Read-only; uses net PnL.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date

from app.services.daily_pnl_service import ClosedRoundTrip


@dataclass(frozen=True)
class EquityPoint:
    date: str  # ISO YYYY-MM-DD
    realized_pnl: float
    cumulative_pnl: float
    drawdown: float
    trade_count: int


@dataclass(frozen=True)
class EquityCurveResult:
    points: list[EquityPoint]
    total_realized_pnl: float
    max_drawdown: float


def compute_equity_curve(trips: list[ClosedRoundTrip]) -> EquityCurveResult:
    # Bucket net realized PnL by the exit-day. exit_at is always tz-aware UTC
    # (coerced by DailyPnlService._fill_from_order), so .date() is the UTC date.
    buckets: dict[date, tuple[float, int]] = {}
    for t in trips:
        day = t.exit_at.date()
        pnl, count = buckets.get(day, (0.0, 0))
        buckets[day] = (pnl + t.net_pnl, count + 1)

    points: list[EquityPoint] = []
    cumulative = 0.0
    peak = 0.0
    max_drawdown = 0.0
    for day in sorted(buckets):
        pnl, count = buckets[day]
        cumulative += pnl
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        max_drawdown = max(max_drawdown, drawdown)
        points.append(EquityPoint(
            date=day.isoformat(),
            realized_pnl=round(pnl, 2),
            cumulative_pnl=round(cumulative, 2),
            drawdown=round(drawdown, 2),
            trade_count=count,
        ))

    return EquityCurveResult(
        points=points,
        total_realized_pnl=round(cumulative, 2),
        max_drawdown=round(max_drawdown, 2),
    )
