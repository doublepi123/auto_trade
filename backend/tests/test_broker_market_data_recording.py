from __future__ import annotations

import threading
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import app.core.broker as broker_module
from app.config import settings
from app.core.broker import BrokerGateway, Quote


class _FakeSubType:
    Quote = "SubType.Quote"
    Depth = "SubType.Depth"
    Trade = "SubType.Trade"


class _FakeModule:
    SubType = _FakeSubType

    class Config:
        @staticmethod
        def from_env() -> str:
            return "fake-config"


class _FakeQuoteContext:
    """Records handler registration and subscribe calls; no SDK connection."""

    instances: list["_FakeQuoteContext"] = []

    def __init__(self, _config: object) -> None:
        self.quote_handler: Any = None
        self.depth_handler: Any = None
        self.trades_handler: Any = None
        self.trade_handler_calls = 0
        self.subscribe_calls: list[tuple[list[str], list[Any]]] = []
        self.unsubscribe_calls: list[tuple[list[str], list[Any]]] = []
        self.fail_trade_subscribe = False
        self.closed = False
        _FakeQuoteContext.instances.append(self)

    def set_on_quote(self, cb: Any) -> None:
        self.quote_handler = cb

    def set_on_depth(self, cb: Any) -> None:
        self.depth_handler = cb

    def set_on_trades(self, cb: Any) -> None:
        self.trade_handler_calls += 1
        self.trades_handler = cb

    def subscribe(self, symbols: list[str], topics: list[Any]) -> None:
        if _FakeSubType.Trade in topics and self.fail_trade_subscribe:
            raise RuntimeError("trade permission denied")
        self.subscribe_calls.append((list(symbols), list(topics)))

    def unsubscribe(self, symbols: list[str], topics: list[Any]) -> None:
        self.unsubscribe_calls.append((list(symbols), list(topics)))

    def close(self) -> None:
        self.closed = True


class _FakeTradeContext:
    def __init__(self, _config: object) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeRecorder:
    """Stands in for MarketDataRecorder; records observe() calls only."""

    def __init__(self, symbols: set[str]) -> None:
        self._symbols = frozenset(symbols)
        self.observed: list[tuple[str, str, Any]] = []
        self.closed_with: float | None = None

    def observe(self, kind: str, symbol: str, event: Any) -> None:
        self.observed.append((kind, symbol, event))

    def configured_symbols(self) -> frozenset[str]:
        return self._symbols

    def snapshot(self) -> dict[str, Any]:
        return {"enabled": True, "state": "WAITING"}

    def close(self, timeout: float = 2.0) -> None:
        self.closed_with = timeout


@pytest.fixture(autouse=True)
def _fresh_contexts(monkeypatch: pytest.MonkeyPatch) -> None:
    _FakeQuoteContext.instances = []
    monkeypatch.setattr(broker_module, "_import_openapi", lambda: _FakeModule)
    monkeypatch.setattr(_FakeModule, "QuoteContext", _FakeQuoteContext, raising=False)
    monkeypatch.setattr(_FakeModule, "TradeContext", _FakeTradeContext, raising=False)


def _gateway(monkeypatch: pytest.MonkeyPatch, enabled: bool, symbols: str = "TSLA.US") -> BrokerGateway:
    monkeypatch.setattr(settings, "market_data_recording_enabled", enabled)
    monkeypatch.setattr(settings, "market_data_recording_symbols", symbols)
    gw = BrokerGateway()
    return gw


def _quote_event(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(
        symbol=symbol, last_done=Decimal("10.5"), timestamp="t",
    )


def _install_fake_recorder(gw: BrokerGateway, symbols: set[str]) -> _FakeRecorder:
    recorder = _FakeRecorder(symbols)
    gw._market_recorder = recorder
    return recorder


def test_off_flag_creates_no_recorder_and_no_trade_handler(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=False)
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    ctx = _FakeQuoteContext.instances[-1]
    assert gw._market_recorder is None
    assert ctx.trade_handler_calls == 0
    assert all(_FakeSubType.Trade not in topics for _, topics in ctx.subscribe_calls)
    assert gw.market_data_recording_snapshot() == {"enabled": False, "state": "OFF"}


def test_trade_subscribed_only_for_whitelisted_symbol_in_mixed_batch(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["NVDA.US", "TSLA.US"], lambda _q: None)
    ctx = _FakeQuoteContext.instances[-1]
    base = [(s, t) for s, t in ctx.subscribe_calls if _FakeSubType.Quote in t]
    trade = [(s, t) for s, t in ctx.subscribe_calls if _FakeSubType.Trade in t]
    assert base == [(["NVDA.US", "TSLA.US"], [_FakeSubType.Quote, _FakeSubType.Depth])]
    assert trade == [(["TSLA.US"], [_FakeSubType.Trade])]
    assert gw._trade_subscribed_symbols == {"TSLA.US"}


def test_trade_handler_registered_exactly_once_across_resubscribes(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    ctx = _FakeQuoteContext.instances[-1]
    assert ctx.trade_handler_calls == 1


def test_trade_failure_keeps_base_subscription_and_quote_delivery(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    received: list[Quote] = []
    gw.subscribe_quotes_batch(["TSLA.US"], received.append)
    ctx = _FakeQuoteContext.instances[-1]
    assert gw._subscribed_symbols == {"TSLA.US"}
    assert len(gw._quote_callbacks) == 1
    # Trade RPC failure on the next resubscribe must not undo the base set.
    ctx.fail_trade_subscribe = True
    gw._trade_subscribed_symbols.clear()
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    assert gw._subscribed_symbols == {"TSLA.US"}
    assert gw._trade_subscribed_symbols == set()
    gw._on_quote_push("TSLA.US", _quote_event("TSLA.US"))
    assert len(received) == 1


def test_quote_depth_callbacks_unchanged_when_recording_on(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    recorder = _install_fake_recorder(gw, {"TSLA.US"})
    received: list[Quote] = []
    gw.subscribe_quotes_batch(["TSLA.US"], received.append)
    gw._on_quote_push("TSLA.US", _quote_event("TSLA.US"))
    assert [q.symbol for q in received] == ["TSLA.US"]
    assert [kind for kind, _s, _e in recorder.observed] == ["quote"]


def test_recorder_observer_failure_never_affects_quote_path(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)

    class _Boom(_FakeRecorder):
        def observe(self, kind: str, symbol: str, event: Any) -> None:
            raise RuntimeError("recorder exploded")

    gw._market_recorder = _Boom({"TSLA.US"})
    received: list[Quote] = []
    gw.subscribe_quotes_batch(["TSLA.US"], received.append)
    gw._on_quote_push("TSLA.US", _quote_event("TSLA.US"))
    assert len(received) == 1


def test_trade_push_records_without_touching_bbo_or_last_trade(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    recorder = _install_fake_recorder(gw, {"TSLA.US"})
    before = (dict(gw._last_trade_by_symbol), dict(gw._bbo_by_symbol))
    gw._on_trades_push("TSLA.US", SimpleNamespace(symbol="TSLA.US", trades=[]))
    assert [kind for kind, _s, _e in recorder.observed] == ["trade"]
    assert (dict(gw._last_trade_by_symbol), dict(gw._bbo_by_symbol)) == before


def test_unsubscribe_resets_both_groups_and_tolerates_trade_failure(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US", "NVDA.US"], lambda _q: None)
    ctx = _FakeQuoteContext.instances[-1]
    gw.unsubscribe_quotes()
    unsubs = [topics for _s, topics in ctx.unsubscribe_calls]
    assert [_FakeSubType.Quote, _FakeSubType.Depth] in [t for t in unsubs]
    assert [_FakeSubType.Trade] in unsubs
    assert gw._subscribed_symbols == set()
    assert gw._trade_subscribed_symbols == set()


def test_close_resets_trade_state_and_closes_recorder_with_bounded_timeout(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    recorder = _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    gw.close()
    assert gw._trades_handler_registered is False
    assert gw._trade_subscribed_symbols == set()
    assert recorder.closed_with == 2.0
    gw.close()  # idempotent: second close must not raise


def test_close_then_resubscribe_registers_new_handler_on_new_context(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    gw.close()
    gw._market_recorder = _FakeRecorder({"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    assert len(_FakeQuoteContext.instances) == 2
    assert _FakeQuoteContext.instances[-1].trade_handler_calls == 1


def test_set_on_trades_runs_without_broker_lock_held(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    lock_held_during_setter: list[bool] = []

    class _Ctx(_FakeQuoteContext):
        def set_on_trades(self, cb: Any) -> None:
            acquired = gw._lock.acquire(blocking=False)
            if acquired:
                gw._lock.release()
            lock_held_during_setter.append(not acquired)
            super().set_on_trades(cb)

    monkeypatch.setattr(_FakeModule, "QuoteContext", _Ctx, raising=False)
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    assert lock_held_during_setter == [False]


def test_recorder_snapshot_reports_subscribed_trade_symbols(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    snap = gw.market_data_recording_snapshot()
    assert snap["subscribed_trade_symbols"] == ["TSLA.US"]
    assert snap["trades_handler_registered"] is True
