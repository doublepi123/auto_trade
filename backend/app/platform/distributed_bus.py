"""P193: cross-process event bus transport layer.

A transport-neutral bridge so a platform ``EventBus`` can fan events out to
other processes (Redis pub/sub, NATS, in-memory for tests). The transport is
optional and dependency-free: callers that never inject one behave exactly like
the plain :class:`~app.platform.bus.EventBus`.

The design mirrors the transport abstraction in Nautilus Trader's message bus
and QuantConnect Lean's ``MessagingBus`` — a pluggable sender for inter-process
events — but stays a pure-Python Protocol so no third-party package is required
at import time.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Protocol, runtime_checkable

from app.platform.bus import EventBus
from app.platform.events import Event, event_from_dict

logger = logging.getLogger(__name__)

ChannelMapper = Callable[[Event], str | None]
"""Decide which cross-process channel an event goes to (None = local only)."""

ChannelTypeMapper = Callable[[str], str | None]
"""Map an ``event_type`` string to a channel (None = do not subscribe remotely)."""


def default_channel_mapper(event: Event) -> str | None:
    """Route events to a ``platform.{event_type}`` channel by type."""
    return f"platform.{event.event_type}"


def default_event_type_mapper(event_type: str) -> str | None:
    """Mirror of :func:`default_channel_mapper` keyed by event type string."""
    return f"platform.{event_type}"


@runtime_checkable
class Transport(Protocol):
    """A best-effort inter-process event transport.

    Implementations must be fault-tolerant: :meth:`publish` and :meth:`consume`
    failures are logged and swallowed — they must never break the local hot path
    (publishing to local subscribers always succeeds regardless of transport).
    """

    def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        """Send ``payload`` on ``channel``.

        Returns ``True`` if the message was accepted by the transport, ``False``
        on any failure (the caller should treat this as best-effort telemetry).
        """
        ...

    def consume(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        """Register ``handler`` for messages arriving on ``channel``."""
        ...

    def close(self) -> None:
        """Release any transport resources (idempotent)."""
        ...


class InMemoryTransport:
    """A transport that re-dispatches within the same process.

    Useful in tests and as the default when no real transport is configured —
    it lets a :class:`DistributedEventBus` round-trip without external state.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, list[Callable[[dict[str, Any]], None]]] = {}

    def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        for handler in list(self._subscribers.get(channel, [])):
            try:
                handler(payload)
            except Exception:  # pragma: no cover - defensive
                logger.exception("in-memory transport handler failed on %s", channel)
        return True

    def consume(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        self._subscribers.setdefault(channel, []).append(handler)

    def close(self) -> None:
        self._subscribers.clear()


class NullTransport:
    """A transport that silently drops every message.

    Used when a real transport is unavailable (e.g. Redis not installed) so the
    rest of the system can keep running with purely-local dispatch.
    """

    def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        return False

    def consume(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        return None

    def close(self) -> None:
        return None


class DistributedEventBus(EventBus):
    """An :class:`EventBus` that also bridges to an optional transport.

    Local subscribers always fire first (synchronous, never swallowed). Then, if
    a channel is resolved for the event and a transport is present, the event's
    dict representation is published best-effort. Messages consumed *from* the
    transport are reconstructed via :func:`~app.platform.events.event_from_dict`
    and dispatched to local subscribers — so a single bus instance acts as both
    a publisher and a subscriber in a distributed setup.
    """

    def __init__(
        self,
        transport: Transport | None = None,
        channel_mapper: ChannelMapper = default_channel_mapper,
        event_type_mapper: ChannelTypeMapper = default_event_type_mapper,
    ) -> None:
        super().__init__()
        self._transport: Transport | None = transport
        self._channel_mapper: ChannelMapper = channel_mapper
        self._event_type_mapper: ChannelTypeMapper = event_type_mapper

    @property
    def transport(self) -> Transport | None:
        return self._transport

    def attach_transport(self, transport: Transport) -> None:
        """Bind a transport after construction (e.g. lazily when Redis appears)."""
        self._transport = transport

    def detach_transport(self) -> None:
        """Drop the transport; subsequent publishes are local-only."""
        self._transport = None

    def publish(self, event: Event) -> None:
        # Local dispatch first — must always succeed regardless of transport.
        super().publish(event)
        transport = self._transport
        if transport is None:
            return
        channel = self._channel_mapper(event)
        if channel is None:
            return
        try:
            transport.publish(channel, event.to_dict())
        except Exception:  # pragma: no cover - defensive
            logger.exception("transport publish failed on channel %s", channel)

    def subscribe_remote(self, event_type: str) -> None:
        """Replay remote messages of ``event_type`` into local subscribers.

        Resolves the channel via the configured event-type mapper and, for each
        payload received, reconstructs the Event and dispatches it to local
        subscribers only (never re-published, to avoid loops). No-op without a
        transport attached.
        """
        transport = self._transport
        if transport is None:
            return
        channel = self._event_type_mapper(event_type)
        if channel is None:
            return

        def _on_message(payload: dict[str, Any]) -> None:
            try:
                reconstructed = event_from_dict(payload)
            except Exception:
                logger.exception("failed to reconstruct remote event on %s", channel)
                return
            # Dispatch only — do NOT re-publish to transport (avoids loops).
            for handler in list(self._handlers.get(reconstructed.event_type, [])):
                try:
                    handler(reconstructed)
                except Exception:  # pragma: no cover - defensive
                    logger.exception("remote handler failed on %s", channel)

        transport.consume(channel, _on_message)


def redis_transport_factory(url: str) -> Transport:
    """Build a Redis pub/sub transport, or :class:`NullTransport` if unavailable.

    Importing ``redis`` is deferred to call time so the module has no hard
    dependency. When the package is missing or the connection fails, a
    :class:`NullTransport` is returned and the caller continues with local-only
    dispatch — mirroring the platform's additive, fail-safe conventions.
    """
    try:
        import redis  # type: ignore[import-not-found]
    except Exception:
        logger.info("redis package unavailable; cross-process bus is null transport")
        return NullTransport()

    try:
        client = redis.Redis.from_url(url)
        client.ping()
    except Exception:
        logger.exception("redis connection failed at %s; falling back to null transport", url)
        return NullTransport()

    return _RedisTransport(client)


class _RedisTransport:
    """Redis pub/sub adapter (returned by :func:`redis_transport_factory`).

    Publishing is synchronous via ``publish``. Subscription uses a background
    ``PubSub`` worker thread owned by the transport and torn down on
    :meth:`close`.
    """

    def __init__(self, client: Any) -> None:
        self._client = client
        self._pubsub = client.pubsub()
        self._started = False

    def publish(self, channel: str, payload: dict[str, Any]) -> bool:
        import json

        try:
            self._client.publish(channel, json.dumps(payload, default=str))
            return True
        except Exception:
            logger.exception("redis publish failed on %s", channel)
            return False

    def consume(self, channel: str, handler: Callable[[dict[str, Any]], None]) -> None:
        import json

        def _decode(message: dict[str, Any]) -> None:
            data = message.get("data")
            if not isinstance(data, (bytes, bytearray)):
                return
            try:
                handler(json.loads(data))
            except Exception:  # pragma: no cover - defensive
                logger.exception("redis message decode failed on %s", channel)

        try:
            self._pubsub.subscribe(**{channel: _decode})
            if not self._started:
                self._pubsub.run_in_thread(sleep_time=0.05, daemon=True)
                self._started = True
        except Exception:
            logger.exception("redis subscribe failed on %s", channel)

    def close(self) -> None:
        try:
            self._pubsub.close()
        except Exception:  # pragma: no cover - defensive
            logger.exception("redis pubsub close failed")
        self._started = False
