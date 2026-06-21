"""Unified platform event model.

All events are frozen dataclasses (kw_only=True), serializable to dict,
and restorable from dict via the EVENT_REGISTRY.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, ClassVar
from uuid import UUID, uuid4


class EventSource(Enum):
    MARKET = "market"
    STRATEGY = "strategy"
    RISK = "risk"
    BROKER = "broker"
    EXECUTION = "execution"
    SYSTEM = "system"


@dataclass(frozen=True, kw_only=True)
class Event:
    """Base event."""

    event_type: ClassVar[str] = "event"

    timestamp: datetime
    source: EventSource
    symbol: str | None = None
    event_id: UUID = uuid4()

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict with JSON-friendly types."""
        result: dict[str, Any] = {"event_type": self.event_type}
        for f in fields(self):
            value = getattr(self, f.name)
            if isinstance(value, Enum):
                result[f.name] = value.value
            elif isinstance(value, datetime):
                result[f.name] = value.isoformat()
            elif isinstance(value, UUID):
                result[f.name] = str(value)
            elif isinstance(value, Decimal):
                result[f.name] = str(value)
            else:
                result[f.name] = value
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Event:
        """Restore from dict. Subclasses should override for specific typing."""
        raise NotImplementedError("Use event_from_dict or subclass from_dict")


@dataclass(frozen=True, kw_only=True)
class QuoteEvent(Event):
    """Market quote tick."""

    event_type: ClassVar[str] = "quote"

    last_price: Decimal
    bid: Decimal | None = None
    ask: Decimal | None = None
    volume: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> QuoteEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            last_price=Decimal(data["last_price"]),
            bid=Decimal(data["bid"]) if data.get("bid") is not None else None,
            ask=Decimal(data["ask"]) if data.get("ask") is not None else None,
            volume=data.get("volume"),
        )


@dataclass(frozen=True, kw_only=True)
class BarEvent(Event):
    """OHLCV bar."""

    event_type: ClassVar[str] = "bar"

    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> BarEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            open=Decimal(data["open"]),
            high=Decimal(data["high"]),
            low=Decimal(data["low"]),
            close=Decimal(data["close"]),
            volume=data["volume"],
        )


@dataclass(frozen=True, kw_only=True)
class SignalEvent(Event):
    """Strategy signal."""

    event_type: ClassVar[str] = "signal"

    signal_type: str
    side: str | None = None
    price: Decimal | None = None
    quantity: int | None = None
    reason: str
    params: dict = None  # type: ignore[assignment]

    def __post_init__(self):
        object.__setattr__(self, "params", self.params or {})

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SignalEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            signal_type=data["signal_type"],
            side=data.get("side"),
            price=Decimal(data["price"]) if data.get("price") is not None else None,
            quantity=data.get("quantity"),
            reason=data["reason"],
            params=data.get("params", {}),
        )


@dataclass(frozen=True, kw_only=True)
class OrderIntentEvent(Event):
    """Intent to place an order (pre-execution)."""

    event_type: ClassVar[str] = "order_intent"

    side: str
    quantity: int
    order_type: str
    limit_price: Decimal | None = None
    reason: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrderIntentEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            side=data["side"],
            quantity=data["quantity"],
            order_type=data["order_type"],
            limit_price=Decimal(data["limit_price"]) if data.get("limit_price") is not None else None,
            reason=data["reason"],
        )


@dataclass(frozen=True, kw_only=True)
class OrderEvent(Event):
    """Order lifecycle update from broker."""

    event_type: ClassVar[str] = "order"

    broker_order_id: str
    status: str
    filled_quantity: int = 0
    avg_price: Decimal | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> OrderEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            broker_order_id=data["broker_order_id"],
            status=data["status"],
            filled_quantity=data.get("filled_quantity", 0),
            avg_price=Decimal(data["avg_price"]) if data.get("avg_price") is not None else None,
        )


@dataclass(frozen=True, kw_only=True)
class FillEvent(Event):
    """Individual fill / execution report."""

    event_type: ClassVar[str] = "fill"

    broker_order_id: str
    side: str
    quantity: int
    price: Decimal
    fee: Decimal = Decimal("0")

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FillEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            broker_order_id=data["broker_order_id"],
            side=data["side"],
            quantity=data["quantity"],
            price=Decimal(data["price"]),
            fee=Decimal(data.get("fee", "0")),
        )


@dataclass(frozen=True, kw_only=True)
class RiskEvent(Event):
    """Risk system event."""

    event_type: ClassVar[str] = "risk"

    risk_type: str
    severity: str
    message: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RiskEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            risk_type=data["risk_type"],
            severity=data["severity"],
            message=data["message"],
        )


@dataclass(frozen=True, kw_only=True)
class ControlEvent(Event):
    """System control command."""

    event_type: ClassVar[str] = "control"

    action: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ControlEvent:
        return cls(
            timestamp=datetime.fromisoformat(data["timestamp"]),
            source=EventSource(data["source"]),
            symbol=data.get("symbol"),
            event_id=UUID(data.get("event_id", str(uuid4()))),
            action=data["action"],
        )


EVENT_REGISTRY: dict[str, type[Event]] = {
    QuoteEvent.event_type: QuoteEvent,
    BarEvent.event_type: BarEvent,
    SignalEvent.event_type: SignalEvent,
    OrderIntentEvent.event_type: OrderIntentEvent,
    OrderEvent.event_type: OrderEvent,
    FillEvent.event_type: FillEvent,
    RiskEvent.event_type: RiskEvent,
    ControlEvent.event_type: ControlEvent,
}


def event_from_dict(data: dict[str, Any]) -> Event:
    """Restore an Event subclass from its dict representation."""
    event_type = data.get("event_type", "event")
    cls = EVENT_REGISTRY.get(event_type)
    if cls is None:
        raise ValueError(f"Unknown event_type: {event_type}")
    return cls.from_dict(data)
