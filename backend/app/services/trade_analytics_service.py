"""Read-only analytics over closed round-trip trades.

All functions operate on ``ClosedRoundTrip`` rows from
``DailyPnlService.pair_round_trips``. They are pure aggregation helpers:
no database access, no broker calls, no runner state.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from app.services.daily_pnl_service import ClosedRoundTrip


@dataclass(frozen=True)
class TradeCalendarDay:
    date: str
    trade_count: int
    win_count: int
    loss_count: int
    net_pnl: float
    gross_pnl: float
    symbols: list[str]


@dataclass(frozen=True)
class HoldDurationBucket:
    bucket: str
    min_seconds: float | None
    max_seconds: float | None
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    avg_net_pnl: float | None


@dataclass(frozen=True)
class PnlDistributionBucket:
    bucket: str
    min_pnl: float | None
    max_pnl: float | None
    trade_count: int
    net_pnl: float


@dataclass(frozen=True)
class MonthlySummaryRow:
    month: str
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    gross_pnl: float
    cumulative_pnl: float
    drawdown: float


@dataclass(frozen=True)
class WeekdayAttributionRow:
    weekday: int
    label: str
    trade_count: int
    win_count: int
    loss_count: int
    win_rate: float
    net_pnl: float
    avg_net_pnl: float | None


_WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
_HOLD_BUCKETS: list[tuple[str, float | None, float | None]] = [
    ("<5m", None, 5 * 60),
    ("5m-1h", 5 * 60, 60 * 60),
    ("1h-1d", 60 * 60, 24 * 60 * 60),
    ("1d-1w", 24 * 60 * 60, 7 * 24 * 60 * 60),
    (">=1w", 7 * 24 * 60 * 60, None),
]
_PNL_BUCKETS: list[tuple[str, float | None, float | None]] = [
    ("<=-200", None, -200.0),
    ("-200--50", -200.0, -50.0),
    ("-50-0", -50.0, 0.0),
    ("breakeven", 0.0, 0.0),
    ("0-200", 0.0, 200.0),
    (">=200", 200.0, None),
]


def compute_trade_calendar(trips: list[ClosedRoundTrip]) -> list[TradeCalendarDay]:
    buckets: dict[str, list[ClosedRoundTrip]] = {}
    for trip in trips:
        buckets.setdefault(trip.exit_at.date().isoformat(), []).append(trip)

    return [
        TradeCalendarDay(
            date=day,
            trade_count=len(rows),
            win_count=sum(1 for row in rows if row.net_pnl > 0),
            loss_count=sum(1 for row in rows if row.net_pnl < 0),
            net_pnl=round(sum(row.net_pnl for row in rows), 2),
            gross_pnl=round(sum(row.gross_pnl for row in rows), 2),
            symbols=sorted({row.symbol for row in rows}),
        )
        for day, rows in sorted(buckets.items())
    ]


def compute_hold_duration_buckets(trips: list[ClosedRoundTrip]) -> list[HoldDurationBucket]:
    result: list[HoldDurationBucket] = []
    for label, minimum, maximum in _HOLD_BUCKETS:
        rows = [
            trip for trip in trips
            if trip.holding_seconds > 0 and _in_range(trip.holding_seconds, minimum, maximum)
        ]
        result.append(HoldDurationBucket(
            bucket=label,
            min_seconds=minimum,
            max_seconds=maximum,
            trade_count=len(rows),
            win_count=sum(1 for row in rows if row.net_pnl > 0),
            loss_count=sum(1 for row in rows if row.net_pnl < 0),
            win_rate=_rate(sum(1 for row in rows if row.net_pnl > 0), len(rows)),
            net_pnl=round(sum(row.net_pnl for row in rows), 2),
            avg_net_pnl=_average(row.net_pnl for row in rows),
        ))
    return result


def compute_pnl_distribution(trips: list[ClosedRoundTrip]) -> list[PnlDistributionBucket]:
    result: list[PnlDistributionBucket] = []
    for label, minimum, maximum in _PNL_BUCKETS:
        rows = [trip for trip in trips if _pnl_in_bucket(trip.net_pnl, label, minimum, maximum)]
        result.append(PnlDistributionBucket(
            bucket=label,
            min_pnl=minimum,
            max_pnl=maximum,
            trade_count=len(rows),
            net_pnl=round(sum(row.net_pnl for row in rows), 2),
        ))
    return result


def compute_monthly_summary(trips: list[ClosedRoundTrip]) -> list[MonthlySummaryRow]:
    buckets: dict[str, list[ClosedRoundTrip]] = {}
    for trip in trips:
        buckets.setdefault(trip.exit_at.strftime("%Y-%m"), []).append(trip)

    rows: list[MonthlySummaryRow] = []
    cumulative = 0.0
    peak = 0.0
    for month, items in sorted(buckets.items()):
        net = sum(item.net_pnl for item in items)
        cumulative += net
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        wins = sum(1 for item in items if item.net_pnl > 0)
        rows.append(MonthlySummaryRow(
            month=month,
            trade_count=len(items),
            win_count=wins,
            loss_count=sum(1 for item in items if item.net_pnl < 0),
            win_rate=_rate(wins, len(items)),
            net_pnl=round(net, 2),
            gross_pnl=round(sum(item.gross_pnl for item in items), 2),
            cumulative_pnl=round(cumulative, 2),
            drawdown=round(drawdown, 2),
        ))
    return rows


def compute_weekday_attribution(trips: list[ClosedRoundTrip]) -> list[WeekdayAttributionRow]:
    buckets: dict[int, list[ClosedRoundTrip]] = {}
    for trip in trips:
        buckets.setdefault(trip.exit_at.weekday(), []).append(trip)

    rows: list[WeekdayAttributionRow] = []
    for weekday in sorted(buckets):
        items = buckets[weekday]
        wins = sum(1 for item in items if item.net_pnl > 0)
        rows.append(WeekdayAttributionRow(
            weekday=weekday,
            label=_WEEKDAY_LABELS[weekday],
            trade_count=len(items),
            win_count=wins,
            loss_count=sum(1 for item in items if item.net_pnl < 0),
            win_rate=_rate(wins, len(items)),
            net_pnl=round(sum(item.net_pnl for item in items), 2),
            avg_net_pnl=_average(item.net_pnl for item in items),
        ))
    return rows


def _in_range(value: float, minimum: float | None, maximum: float | None) -> bool:
    return (minimum is None or value >= minimum) and (maximum is None or value < maximum)


def _pnl_in_bucket(value: float, label: str, minimum: float | None, maximum: float | None) -> bool:
    if label == "breakeven":
        return value == 0
    if minimum is None:
        return maximum is not None and value <= maximum
    if maximum is None:
        return value >= minimum
    if maximum == 0.0:
        return minimum < value < maximum
    if minimum == 0.0:
        return minimum < value < maximum
    return minimum <= value <= maximum if maximum < 0 else minimum <= value < maximum


def _rate(part: int, total: int) -> float:
    return round((part / total) * 100.0, 4) if total else 0.0


def _average(values: Iterable[float]) -> float | None:
    items = list(values)
    if not items:
        return None
    return round(sum(items) / len(items), 2)
