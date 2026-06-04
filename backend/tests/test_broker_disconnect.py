# pyright: reportPrivateUsage=false
from __future__ import annotations

from collections.abc import Callable

from app.core.broker import BrokerGateway


class _FakeQuoteContext:
    """Inline fake QuoteContext for SDK disconnect behavior."""

    def __init__(self, supports_disconnect: bool = False) -> None:
        self._disconnect_handler: Callable[[str], None] | None = None
        if supports_disconnect:
            self.on_disconnect = self._on_disconnect

    def _on_disconnect(self, handler: Callable[[str], None]) -> Callable[[str], None]:
        self._disconnect_handler = handler
        return handler

    def simulate_disconnect(self, reason: str) -> None:
        if self._disconnect_handler is not None:
            self._disconnect_handler(reason)


def _gateway_with_quote_context(ctx: _FakeQuoteContext) -> BrokerGateway:
    gw = BrokerGateway()
    gw._quote_ctx = ctx
    gw._register_native_disconnect_if_available()
    return gw


def test_register_disconnect_hook_stores_callable() -> None:
    gw = _gateway_with_quote_context(_FakeQuoteContext())
    calls: list[str] = []

    gw.register_disconnect_hook(lambda reason: calls.append(reason))

    assert len(gw._disconnect_hooks) == 1


def test_call_disconnect_hooks_invokes_all() -> None:
    gw = _gateway_with_quote_context(_FakeQuoteContext())
    a: list[str] = []
    b: list[str] = []

    gw.register_disconnect_hook(lambda reason: a.append(reason))
    gw.register_disconnect_hook(lambda reason: b.append(reason))
    gw._call_disconnect_hooks("network_drop")

    assert a == ["network_drop"]
    assert b == ["network_drop"]


def test_call_disconnect_hooks_swallows_exceptions() -> None:
    gw = _gateway_with_quote_context(_FakeQuoteContext())
    calls: list[str] = []

    def broken_hook(_reason: str) -> None:
        raise RuntimeError("boom")

    gw.register_disconnect_hook(broken_hook)
    gw.register_disconnect_hook(lambda reason: calls.append(reason))
    gw._call_disconnect_hooks("x")

    assert calls == ["x"]


def test_quote_context_with_disconnect_event_triggers_hook() -> None:
    ctx = _FakeQuoteContext(supports_disconnect=True)
    gw = _gateway_with_quote_context(ctx)
    calls: list[str] = []

    gw.register_disconnect_hook(lambda reason: calls.append(reason))
    ctx.simulate_disconnect("auth_revoked")

    assert calls == ["auth_revoked"]


def test_quote_context_without_disconnect_event_does_not_break() -> None:
    gw = _gateway_with_quote_context(_FakeQuoteContext(supports_disconnect=False))

    assert gw._disconnect_hooks == []
