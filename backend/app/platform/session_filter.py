from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from typing import Protocol, runtime_checkable

__all__ = ["SessionFilter", "MarketSessionFilter"]


@runtime_checkable
class SessionFilter(Protocol):
    """交易时段过滤（参考 Nautilus TradingSession / Lean MarketHoursDatabase）。"""

    def session_at(self, timestamp: datetime) -> str: ...  # pre/rth/post/closed

    def allows(self, timestamp: datetime, allowed_sessions: tuple[str, ...]) -> bool: ...


def _in_window(t: time, start: time, end: time) -> bool:
    if start <= end:
        return start <= t < end
    # overnight wrap (not typical for equities, but supported)
    return t >= start or t < end


@dataclass(frozen=True)
class MarketSessionFilter:
    """显式时段窗口定义（本地时间）。周末一律 closed。

    rth_window 必填；pre/post 可选。窗口右端为开区间。
    """

    rth_window: tuple[time, time]
    pre_window: tuple[time, time] | None = None
    post_window: tuple[time, time] | None = None

    def session_at(self, timestamp: datetime) -> str:
        if timestamp.weekday() >= 5:  # 5=Sat, 6=Sun
            return "closed"
        t = timestamp.time()
        if self.pre_window and _in_window(t, self.pre_window[0], self.pre_window[1]):
            return "pre"
        if _in_window(t, self.rth_window[0], self.rth_window[1]):
            return "rth"
        if self.post_window and _in_window(t, self.post_window[0], self.post_window[1]):
            return "post"
        return "closed"

    def allows(self, timestamp: datetime, allowed_sessions: tuple[str, ...]) -> bool:
        return self.session_at(timestamp) in allowed_sessions
