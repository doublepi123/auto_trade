"""Tests for the P193 cross-process event bus transport layer."""

from datetime import datetime, timezone
from decimal import Decimal

from app.platform.distributed_bus import (
    DistributedEventBus,
    InMemoryTransport,
    NullTransport,
    default_channel_mapper,
    default_event_type_mapper,
    redis_transport_factory,
)
from app.platform.events import BarEvent, EventSource


def _bar(symbol: str = "AAPL.US", close: str = "150.5") -> BarEvent:
    return BarEvent(
        timestamp=datetime(2026, 6, 22, 10, 0, tzinfo=timezone.utc),
        source=EventSource.MARKET,
        symbol=symbol,
        open=Decimal("150"),
        high=Decimal("151"),
        low=Decimal("149"),
        close=Decimal(close),
        volume=100,
    )


def test_distributed_bus_without_transport_behaves_locally():
    bus = DistributedEventBus()
    received = []
    bus.subscribe("bar", lambda e: received.append(e))

    bus.publish(_bar())

    assert len(received) == 1
    assert received[0].close == Decimal("150.5")


def test_distributed_bus_publishes_to_transport():
    transport = InMemoryTransport()
    bus = DistributedEventBus(transport=transport)
    captured: list[dict] = []
    transport.consume("platform.bar", lambda payload: captured.append(payload))

    bus.publish(_bar())

    assert len(captured) == 1
    assert captured[0]["event_type"] == "bar"
    assert captured[0]["symbol"] == "AAPL.US"
    assert captured[0]["close"] == "150.5"


def test_local_dispatch_precedes_and_survives_transport():
    transport = InMemoryTransport()
    bus = DistributedEventBus(transport=transport)
    local_order: list[str] = []

    bus.subscribe("bar", lambda e: local_order.append("local"))
    transport.consume("platform.bar", lambda p: local_order.append("transport"))

    bus.publish(_bar())

    assert local_order == ["local", "transport"]


def test_channel_mapper_none_skips_transport():
    transport = InMemoryTransport()
    captured: list[dict] = []
    transport.consume("platform.bar", lambda p: captured.append(p))
    bus = DistributedEventBus(transport=transport, channel_mapper=lambda e: None)

    bus.publish(_bar())

    assert captured == []


def test_subscribe_remote_dispatches_reconstructed_event():
    transport = InMemoryTransport()
    receiver = DistributedEventBus(transport=transport)
    reconstructed = []
    receiver.subscribe("bar", lambda e: reconstructed.append(e))
    receiver.subscribe_remote("bar")

    # Publish via a second bus sharing the same transport (cross-process sim).
    sender = DistributedEventBus(transport=transport)
    sender.publish(_bar(close="151.25"))

    assert len(reconstructed) == 1
    assert reconstructed[0].close == Decimal("151.25")
    assert reconstructed[0].symbol == "AAPL.US"


def test_subscribe_remote_noop_without_transport():
    bus = DistributedEventBus()  # no transport
    # Should not raise.
    bus.subscribe_remote("bar")


def test_subscribe_remote_does_not_republish_loop():
    """A reconstructed remote event must not be re-sent to the transport."""
    transport = InMemoryTransport()
    forwarded: list[dict] = []
    transport.consume("platform.bar", lambda p: forwarded.append(p))

    receiver = DistributedEventBus(transport=transport)
    receiver.subscribe("bar", lambda e: None)
    receiver.subscribe_remote("bar")

    sender = DistributedEventBus(transport=transport)
    sender.publish(_bar())

    # Only the original sender publish lands on the transport; the receiver's
    # local-only dispatch of the reconstructed event must not republish.
    assert len(forwarded) == 1


def test_attach_and_detach_transport():
    bus = DistributedEventBus()
    assert bus.transport is None

    transport = InMemoryTransport()
    bus.attach_transport(transport)
    assert bus.transport is transport

    bus.detach_transport()
    assert bus.transport is None


def test_null_transport_drops_messages():
    bus = DistributedEventBus(transport=NullTransport())
    received = []
    bus.subscribe("bar", lambda e: received.append(e))

    bus.publish(_bar())

    # Local subscribers still fire; null transport just drops the remote copy.
    assert len(received) == 1


def test_default_mappers_round_trip():
    bar = _bar()
    assert default_channel_mapper(bar) == "platform.bar"
    assert default_event_type_mapper("bar") == "platform.bar"
    assert default_event_type_mapper("quote") == "platform.quote"


def test_redis_factory_falls_back_to_null_without_package(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def _block_redis(name, *args, **kwargs):
        if name == "redis":
            raise ImportError("simulated absence")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _block_redis)

    transport = redis_transport_factory("redis://localhost:6379/0")

    assert isinstance(transport, NullTransport)
    assert transport.publish("any", {}) is False
