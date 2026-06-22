from __future__ import annotations

from datetime import datetime

from app.platform.bus import EventBus
from app.platform.events import Event
from app.platform.store import EventStore


class EventReplayer:
    """Replays persisted events into an optional bus, enabling deterministic replay."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def replay(
        self,
        since: datetime | None = None,
        symbol: str | None = None,
        limit: int = 10000,
        bus: EventBus | None = None,
    ) -> list[Event]:
        """Load events from the store and publish them to ``bus`` if provided.

        Returns the loaded events without mutation.
        """
        events = self._store.load(since=since, symbol=symbol, limit=limit)
        if bus is not None:
            for event in events:
                bus.publish(event)
        return events
