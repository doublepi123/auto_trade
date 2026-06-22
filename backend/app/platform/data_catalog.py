from __future__ import annotations

from typing import Any

from app.platform.events import BarEvent, Event, EventSource
from app.platform.store import EventStore

__all__ = ["DataCatalog", "resample_bars"]


def resample_bars(bars: list[BarEvent], target_minutes: int) -> list[BarEvent]:
    """将 1m（或更细）bar 按时间桶聚合到 target_minutes 周期（参考 Nautilus BarAggregation）。

    target_minutes <= 1 时原样返回拷贝。
    """
    if target_minutes <= 1 or not bars:
        return list(bars)
    ordered = sorted(bars, key=lambda b: b.timestamp)
    buckets: dict[int, list[BarEvent]] = {}
    for bar in ordered:
        epoch_min = int(bar.timestamp.timestamp()) // 60
        bucket = epoch_min - (epoch_min % target_minutes)
        buckets.setdefault(bucket, []).append(bar)
    out: list[BarEvent] = []
    for bucket in sorted(buckets):
        group = buckets[bucket]
        out.append(
            BarEvent(
                timestamp=group[0].timestamp,
                source=EventSource.MARKET,
                symbol=group[0].symbol,
                open=group[0].open,
                high=max((g.high for g in group), default=group[0].high),
                low=min((g.low for g in group), default=group[0].low),
                close=group[-1].close,
                volume=sum(int(g.volume) for g in group),
            )
        )
    return out


class DataCatalog:
    """从 EventStore 读取历史 BarEvent 并可选重采样（参考 Nautilus DataEngine / Lean History）。"""

    def __init__(self, store: EventStore | None = None) -> None:
        self.store = store or EventStore()

    def load_bars(
        self,
        symbol: str,
        since: Any = None,
        limit: int = 1000,
        resolution_minutes: int = 1,
    ) -> list[BarEvent]:
        events: list[Event] = self.store.load(since=since, symbol=symbol, limit=limit)
        bars = [e for e in events if isinstance(e, BarEvent)]
        return resample_bars(bars, resolution_minutes)
