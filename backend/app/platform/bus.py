from __future__ import annotations

from collections import defaultdict
from typing import Callable

from app.platform.events import Event

Handler = Callable[[Event], None]


class EventBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list[Handler]] = defaultdict(list)

    def subscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type].append(handler)

    def unsubscribe(self, event_type: str, handler: Handler) -> None:
        self._handlers[event_type] = [h for h in self._handlers[event_type] if h is not handler]

    def publish(self, event: Event) -> None:
        for handler in list(self._handlers.get(event.event_type, [])):
            handler(event)

    def clear(self) -> None:
        self._handlers.clear()
