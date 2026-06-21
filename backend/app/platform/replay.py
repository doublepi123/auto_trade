from __future__ import annotations

from datetime import datetime

from app.platform.bus import EventBus
from app.platform.store import EventStore


class EventReplayer:
    def __init__(self, store: EventStore) -> None:
        self._store = store

    def replay(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
        limit: int = 10000,
        bus: EventBus | None = None,
    ) -> list:
        target_bus = bus or EventBus()
        events = self._store.load(since=since, symbol=symbol, limit=limit)
        for event in events:
            target_bus.publish(event)
        return events
