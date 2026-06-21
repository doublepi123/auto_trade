from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class EventSource(Enum):
    MARKET = "market"
    STRATEGY = "strategy"
    BROKER = "broker"
    PLATFORM = "platform"


@dataclass(frozen=True)
class Event:
    timestamp: datetime
    source: EventSource


@dataclass(frozen=True)
class BarEvent(Event):
    symbol: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int


@dataclass(frozen=True)
class QuoteEvent(Event):
    symbol: str
    bid: Decimal | None = None
    ask: Decimal | None = None
    last: Decimal | None = None


@dataclass(frozen=True)
class OrderIntentEvent(Event):
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal | None = None
    reason: str | None = None


@dataclass(frozen=True)
class OrderEvent(Event):
    symbol: str
    side: str
    quantity: int
    order_type: str
    limit_price: Decimal | None = None
    status: str = "SUBMITTED"


@dataclass(frozen=True)
class FillEvent(Event):
    symbol: str
    side: str
    quantity: int
    price: Decimal
    order_id: str | None = None
