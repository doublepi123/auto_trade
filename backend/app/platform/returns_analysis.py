"""P220: Returns Distribution & Calendar Heatmap Analysis.

Aggregate a daily return series into the calendar tables that pyfolio /
QuantStats render as heatmaps:

* **monthly** — compounded gross return per (year, month), with best/worst day.
* **yearly** — compounded gross return per year.
* **weekday** — mean return, win rate, best/worst per weekday (Mon..Fri).
* **streaks** — longest win / loss streak, current streak.
* **summary** — overall win rate, best/worst day (+ date), avg day, dropped NaNs.

Pure compute on arrays (no DB, no new deps). When no dates are supplied, a
deterministic synthetic business-day calendar starting Monday 2000-01-03 is
synthesized so the function is usable with a bare returns list (e.g. backtest
equity-derived daily returns).

Reference: pyfolio ``timeseries.returns_table`` / ``plot_returns``; QuantStats
``stats.distribution``.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Sequence

__all__ = [
    "MonthBucket",
    "YearBucket",
    "ReturnsCalendar",
    "returns_calendar",
    "returns_calendar_dict",
    "monthly_returns_table",
]


@dataclass(frozen=True)
class MonthBucket:
    year: int
    month: int
    gross_return: float
    n_days: int
    best_day: float | None
    worst_day: float | None


@dataclass(frozen=True)
class YearBucket:
    year: int
    gross_return: float
    n_days: int


@dataclass(frozen=True)
class ReturnsCalendar:
    monthly: list[MonthBucket]
    yearly: list[YearBucket]
    weekday: list[dict[str, Any]]
    streaks: dict[str, Any]
    summary: dict[str, Any]


_SYNTH_EPOCH = date(2000, 1, 3)  # Monday


def _coerce_date(x: Any) -> date:
    if isinstance(x, datetime):
        return x.date()
    if isinstance(x, date):
        return x
    if isinstance(x, str):
        return date.fromisoformat(x[:10])
    raise ValueError(f"cannot coerce {x!r} to date")


def _coerce_dates(dates: Sequence[Any] | None, n: int) -> list[date]:
    if dates is None:
        return [_SYNTH_EPOCH.fromordinal(_SYNTH_EPOCH.toordinal() + i) for i in range(n)]
    if len(dates) != n:
        raise ValueError(f"dates length {len(dates)} != returns length {n}")
    return [_coerce_date(d) for d in dates]


def returns_calendar(
    returns: Sequence[float],
    dates: Sequence[Any] | None = None,
) -> ReturnsCalendar:
    """Compute monthly / yearly / weekday / streak aggregates from returns."""
    # filter NaN/inf
    clean: list[tuple[float, date]] = []
    n_dropped = 0
    coerced_dates = _coerce_dates(dates, len(returns)) if len(returns) > 0 else []
    for i, r in enumerate(returns):
        if isinstance(r, float) and not math.isfinite(r):
            n_dropped += 1
            continue
        clean.append((float(r), coerced_dates[i]))

    if not clean:
        return ReturnsCalendar(
            monthly=[], yearly=[], weekday=_empty_weekday(),
            streaks={"max_win_streak": 0, "max_loss_streak": 0, "current_streak": 0, "current_kind": "flat"},
            summary={"n_days": 0, "win_rate": 0.0, "best_day": None, "worst_day": None,
                     "best_day_date": None, "worst_day_date": None, "avg_day": 0.0, "n_dropped": n_dropped},
        )

    # monthly
    monthly: list[MonthBucket] = []
    monthly_groups: dict[tuple[int, int], list[float]] = {}
    for r, d in clean:
        monthly_groups.setdefault((d.year, d.month), []).append(r)
    for (y, m), rs in sorted(monthly_groups.items()):
        gross = 1.0
        for r in rs:
            gross *= (1.0 + r)
        monthly.append(MonthBucket(
            year=y, month=m, gross_return=gross - 1.0, n_days=len(rs),
            best_day=max(rs) if rs else None, worst_day=min(rs) if rs else None,
        ))

    # yearly
    yearly: list[YearBucket] = []
    yearly_groups: dict[int, list[float]] = {}
    for r, d in clean:
        yearly_groups.setdefault(d.year, []).append(r)
    for y, rs in sorted(yearly_groups.items()):
        gross = 1.0
        for r in rs:
            gross *= (1.0 + r)
        yearly.append(YearBucket(year=y, gross_return=gross - 1.0, n_days=len(rs)))

    # weekday (Mon=0 .. Fri=4)
    weekday = _empty_weekday()
    weekday_buckets: dict[int, list[float]] = {i: [] for i in range(5)}
    for r, d in clean:
        wd = d.weekday()
        if wd < 5:
            weekday_buckets[wd].append(r)
    names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    for wd in range(5):
        rs = weekday_buckets[wd]
        if rs:
            mean_r = sum(rs) / len(rs)
            wins = sum(1 for r in rs if r > 0)
            weekday[wd] = {
                "weekday": names[wd],
                "n": len(rs),
                "mean_return": mean_r,
                "win_rate": wins / len(rs),
                "best": max(rs),
                "worst": min(rs),
            }
        else:
            weekday[wd] = {"weekday": names[wd], "n": 0, "mean_return": 0.0,
                          "win_rate": 0.0, "best": None, "worst": None}

    # streaks
    max_win = 0
    max_loss = 0
    cur = 0
    cur_kind = "flat"
    for r, _d in clean:
        if r > 0:
            if cur_kind == "win":
                cur += 1
            else:
                cur = 1
                cur_kind = "win"
            max_win = max(max_win, cur)
        elif r < 0:
            if cur_kind == "loss":
                cur += 1
            else:
                cur = 1
                cur_kind = "loss"
            max_loss = max(max_loss, cur)
        else:
            cur = 0
            cur_kind = "flat"

    # summary
    all_rs = [r for r, _ in clean]
    wins = sum(1 for r in all_rs if r > 0)
    nonflat = sum(1 for r in all_rs if r != 0)
    win_rate = (wins / nonflat) if nonflat > 0 else 0.0
    best = max(all_rs)
    worst = min(all_rs)
    best_date = next(d for r, d in clean if r == best)
    worst_date = next(d for r, d in clean if r == worst)
    avg = sum(all_rs) / len(all_rs)

    return ReturnsCalendar(
        monthly=monthly,
        yearly=yearly,
        weekday=weekday,
        streaks={"max_win_streak": max_win, "max_loss_streak": max_loss,
                 "current_streak": cur, "current_kind": cur_kind},
        summary={"n_days": len(clean), "win_rate": win_rate, "best_day": best,
                 "worst_day": worst, "best_day_date": best_date.isoformat(),
                 "worst_day_date": worst_date.isoformat(), "avg_day": avg,
                 "n_dropped": n_dropped},
    )


def _empty_weekday() -> list[dict[str, Any]]:
    names = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    return [{"weekday": n, "n": 0, "mean_return": 0.0, "win_rate": 0.0,
             "best": None, "worst": None} for n in names]


def returns_calendar_dict(
    returns: Sequence[float],
    dates: Sequence[Any] | None = None,
) -> dict[str, Any]:
    """JSON-friendly dict view of :func:`returns_calendar`."""
    cal = returns_calendar(returns, dates)
    return {
        "monthly": [
            {"year": m.year, "month": m.month, "gross_return": m.gross_return,
             "n_days": m.n_days, "best_day": m.best_day, "worst_day": m.worst_day}
            for m in cal.monthly
        ],
        "yearly": [
            {"year": y.year, "gross_return": y.gross_return, "n_days": y.n_days}
            for y in cal.yearly
        ],
        "weekday": cal.weekday,
        "streaks": cal.streaks,
        "summary": cal.summary,
        "monthly_table": monthly_returns_table(cal.monthly),
    }


def monthly_returns_table(monthly: list[MonthBucket]) -> dict[int, dict[int, float | None]]:
    """Pivot monthly buckets into ``{year: {month: gross_return | None}}`` (gaps → None)."""
    table: dict[int, dict[int, float | None]] = {}
    for m in monthly:
        table.setdefault(m.year, {i: None for i in range(1, 13)})[m.month] = m.gross_return
    return table