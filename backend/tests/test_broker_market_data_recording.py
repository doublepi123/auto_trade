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
    probe_results: list[bool] = []

    class _Ctx(_FakeQuoteContext):
        def set_on_trades(self, cb: Any) -> None:
            # RLock is re-entrant, so probing from the setter's own thread
            # always succeeds. Probe from another thread instead.
            def probe() -> None:
                got = gw._lock.acquire(blocking=False)
                if got:
                    gw._lock.release()
                probe_results.append(got)

            t = threading.Thread(target=probe, daemon=True)
            t.start()
            t.join(timeout=2.0)
            super().set_on_trades(cb)

    monkeypatch.setattr(_FakeModule, "QuoteContext", _Ctx, raising=False)
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    assert probe_results == [True]


class _OrderRecordingContext(_FakeQuoteContext):
    """Logs every setter and subscribe call in one ordered list."""

    log: list[str] = []

    def set_on_quote(self, cb: Any) -> None:
        _OrderRecordingContext.log.append("set_on_quote")
        super().set_on_quote(cb)

    def set_on_depth(self, cb: Any) -> None:
        _OrderRecordingContext.log.append("set_on_depth")
        super().set_on_depth(cb)

    def set_on_trades(self, cb: Any) -> None:
        _OrderRecordingContext.log.append("set_on_trades")
        super().set_on_trades(cb)

    def subscribe(self, symbols: list[str], topics: list[Any]) -> None:
        kind = "trade_subscribe" if _FakeSubType.Trade in topics else "base_subscribe"
        _OrderRecordingContext.log.append(kind)
        super().subscribe(symbols, topics)


def test_all_handlers_registered_before_first_subscribe_when_recording_on(monkeypatch) -> None:
    _OrderRecordingContext.log = []
    monkeypatch.setattr(_FakeModule, "QuoteContext", _OrderRecordingContext, raising=False)
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    log = _OrderRecordingContext.log
    first_subscribe = min(log.index("base_subscribe"), log.index("trade_subscribe"))
    for setter in ("set_on_quote", "set_on_depth", "set_on_trades"):
        assert log.index(setter) < first_subscribe, log
    assert log.index("trade_subscribe") > log.index("base_subscribe"), log


def test_off_path_never_calls_set_on_trades(monkeypatch) -> None:
    _OrderRecordingContext.log = []
    monkeypatch.setattr(_FakeModule, "QuoteContext", _OrderRecordingContext, raising=False)
    gw = _gateway(monkeypatch, enabled=False)
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    assert "set_on_trades" not in _OrderRecordingContext.log
    assert "trade_subscribe" not in _OrderRecordingContext.log


class _FakeCallbackContext(_FakeQuoteContext):
    """Mimics the SDK: push delivery takes callbacks_mutex, then the GIL;
    setters hold the GIL and take callbacks_mutex. Acquire order is
    therefore inverted between the two sides. Uses timeouts so an
    inversion fails the test instead of hanging it."""

    def __init__(self, _config: object) -> None:
        super().__init__(_config)
        self.callbacks_mutex = threading.Lock()
        self.simulated_gil = threading.Lock()
        self.push_thread: threading.Thread | None = None
        self.push_holding_mutex = threading.Event()
        self.release_push = threading.Event()
        self.push_failed: list[str] = []
        self.setter_failed: list[str] = []

    def _push_once(self) -> None:
        # SDK push path: take callbacks_mutex, then the GIL to run Python.
        # The mutex stays held while the push is in flight, as the SDK does.
        if not self.callbacks_mutex.acquire(timeout=1.0):
            self.push_failed.append("callbacks_mutex")
            return
        self.push_holding_mutex.set()
        try:
            # Real SDK: the dispatch thread keeps callbacks_mutex until it
            # owns the GIL. Hold the mutex until the test releases the gate.
            self.release_push.wait(timeout=3.0)
            if not self.simulated_gil.acquire(timeout=1.0):
                self.push_failed.append("gil")
                return
            self.simulated_gil.release()
        finally:
            self.callbacks_mutex.release()

    def _setter(self, name: str) -> None:
        # SDK setter: the caller holds the GIL, then takes callbacks_mutex.
        if not self.simulated_gil.acquire(timeout=1.0):
            self.setter_failed.append(name)
            raise RuntimeError(f"{name} could not get GIL")
        try:
            if not self.callbacks_mutex.acquire(timeout=1.0):
                self.setter_failed.append(name)
                raise RuntimeError(f"{name} deadlocked against push path")
            self.callbacks_mutex.release()
        finally:
            self.simulated_gil.release()

    def set_on_quote(self, cb: Any) -> None:
        self._setter("set_on_quote")
        super().set_on_quote(cb)

    def set_on_depth(self, cb: Any) -> None:
        self._setter("set_on_depth")
        super().set_on_depth(cb)

    def set_on_trades(self, cb: Any) -> None:
        self._setter("set_on_trades")
        super().set_on_trades(cb)

    def subscribe(self, symbols: list[str], topics: list[Any]) -> None:
        super().subscribe(symbols, topics)
        # SDK sends an immediate first push after subscribe. Model it as a
        # push that already owns callbacks_mutex while it waits for the GIL.
        self.push_thread = threading.Thread(target=self._push_once, daemon=True)
        self.push_thread.start()
        self.push_holding_mutex.wait(timeout=1.0)


def test_setters_after_push_started_deadlock_detected_and_avoided(monkeypatch) -> None:
    monkeypatch.setattr(_FakeModule, "QuoteContext", _FakeCallbackContext, raising=False)
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    try:
        gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
        ctx = _FakeQuoteContext.instances[-1]
        assert isinstance(ctx, _FakeCallbackContext)
        assert ctx.setter_failed == [], f"setter after push began: {ctx.setter_failed}"
    finally:
        for ctx in _FakeQuoteContext.instances:
            if isinstance(ctx, _FakeCallbackContext):
                ctx.release_push.set()
                if ctx.push_thread is not None:
                    ctx.push_thread.join(timeout=3.0)


def test_recorder_snapshot_reports_subscribed_trade_symbols(monkeypatch) -> None:
    gw = _gateway(monkeypatch, enabled=True)
    _install_fake_recorder(gw, {"TSLA.US"})
    gw.subscribe_quotes_batch(["TSLA.US"], lambda _q: None)
    snap = gw.market_data_recording_snapshot()
    assert snap["subscribed_trade_symbols"] == ["TSLA.US"]
    assert snap["trades_handler_registered"] is True
