from __future__ import annotations

from datetime import datetime
from typing import Any


class EventStore:
    """Simple in-memory event store with time-based loading."""

    def __init__(self) -> None:
        self._events: list[Any] = []

    def append(self, event: Any) -> None:
        self._events.append(event)

    def load(self, since: datetime | None = None) -> list[Any]:
        if since is None:
            return list(self._events)
        return [e for e in self._events if getattr(e, "timestamp", datetime.min) >= since]

    def clear(self) -> None:
        self._events.clear()
