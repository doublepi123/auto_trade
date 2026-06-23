from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, time
from typing import Callable

from app.platform.events import BarEvent

__all__ = ["Scheduler"]

BarCallback = Callable[[BarEvent], None]


@dataclass
class _BarJob:
    period: int
    callback: BarCallback
    counter: int = 0


@dataclass
class _DailyJob:
    target: time
    callback: BarCallback
    last_fired_date: date | None = None


@dataclass
class Scheduler:
    """策略调度器（参考 Lean ScheduledEvent / Backtrader timer）：在 on_bar 上按周期/时刻触发回调。"""

    bar_jobs: list[_BarJob] = field(default_factory=list)
    daily_jobs: list[_DailyJob] = field(default_factory=list)

    def every_bars(self, period: int, callback: BarCallback) -> None:
        if period < 1:
            raise ValueError("period must be >= 1")
        self.bar_jobs.append(_BarJob(period=period, callback=callback))

    def daily_at(self, hour: int, minute: int, callback: BarCallback) -> None:
        self.daily_jobs.append(_DailyJob(target=time(hour, minute), callback=callback))

    def on_bar(self, bar: BarEvent) -> None:
        for job in self.bar_jobs:
            job.counter += 1
            if job.counter >= job.period:
                job.counter = 0
                job.callback(bar)
        bar_dt = bar.timestamp
        for job in self.daily_jobs:
            today = bar_dt.date()
            if job.last_fired_date == today:
                continue
            # fire when bar's wall-clock time has reached/passed the target today
            if bar_dt.time() >= job.target:
                job.last_fired_date = today
                job.callback(bar)
