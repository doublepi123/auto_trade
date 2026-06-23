from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from app.platform.events import BarEvent, EventSource
from app.platform.scheduler import Scheduler


def _bar(hour: int, minute: int, day: int = 23) -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, day, hour, minute, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol="A",
        open=Decimal("1"),
        high=Decimal("1"),
        low=Decimal("1"),
        close=Decimal("1"),
        volume=1,
    )


def test_every_bars_fires_at_period():
    fired: list[BarEvent] = []
    sched = Scheduler()
    sched.every_bars(3, lambda b: fired.append(b))
    for i in range(7):
        sched.on_bar(_bar(10, i))
    # fires at bar index 3 (counter reaches 3) and bar index 6
    assert len(fired) == 2


def test_every_bars_invalid_period_raises():
    sched = Scheduler()
    with pytest.raises(ValueError):
        sched.every_bars(0, lambda b: None)


def test_daily_at_fires_once_per_day():
    fired: list[BarEvent] = []
    sched = Scheduler()
    sched.daily_at(10, 30, lambda b: fired.append(b))
    sched.on_bar(_bar(10, 0))        # before target -> no
    sched.on_bar(_bar(10, 30))       # at target -> fire
    sched.on_bar(_bar(10, 45))       # later same day -> no (already fired today)
    sched.on_bar(_bar(11, 0, day=24))  # next day at 11:00 -> fire (new day, time >= target)
    assert len(fired) == 2
