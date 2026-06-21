"""Platform plugin SDK and unified event stream."""

from __future__ import annotations

from app.platform.bus import EventBus
from app.platform.context import StrategyContext
from app.platform.events import (
    BarEvent,
    ControlEvent,
    Event,
    EventSource,
    FillEvent,
    OrderEvent,
    OrderIntentEvent,
    QuoteEvent,
    RiskEvent,
    SignalEvent,
    event_from_dict,
)
from app.platform.registry import StrategyMeta, StrategyRegistry, get_default_registry
from app.platform.replay import EventReplayer
from app.platform.runner import PlatformRunner
from app.platform.sdk import OrderIntent, Strategy
from app.platform.simbroker import SimBroker
from app.platform.store import EventStore

__all__ = [
    "BarEvent",
    "ControlEvent",
    "Event",
    "EventBus",
    "EventSource",
    "EventStore",
    "FillEvent",
    "OrderEvent",
    "OrderIntent",
    "OrderIntentEvent",
    "PlatformRunner",
    "QuoteEvent",
    "RiskEvent",
    "SignalEvent",
    "Strategy",
    "StrategyContext",
    "StrategyMeta",
    "StrategyRegistry",
    "SimBroker",
    "event_from_dict",
    "get_default_registry",
    "EventReplayer",
]
