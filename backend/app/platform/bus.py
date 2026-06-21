from __future__ import annotations

from typing import Any, Callable


class EventBus:
    """Simple in-memory publish-subscribe event bus."""

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[Any], None]]] = {}

    def subscribe(self, event_type: str, handler: Callable[[Any], None]) -> None:
        self._subscribers.setdefault(event_type, []).append(handler)

    def publish(self, event: Any) -> None:
        event_type = getattr(event, "event_type", None)
        if event_type is None:
            event_type = type(event).__name__.replace("Event", "").lower()
        handlers = self._subscribers.get(event_type, [])
        for handler in handlers:
            handler(event)
