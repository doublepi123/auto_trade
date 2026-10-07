"""Fill-tail callbacks must not hold TES._state_lock.

The live ABBA deadlock was: the reconcile thread held TES._state_lock inside
the fill tail and then entered a runner callback that takes runner._state_lock,
while a quote thread already held runner._state_lock and waited on
TES._state_lock (pending_order_for).

Callbacks must not assert or time out. ``_mark_fill_processed`` and
``_notify_reduction_fill`` swallow exceptions, which would hide a deadlock
and let the finalizer return.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from decimal import Decimal

import pytest

from app.core.broker import BrokerGateway
from app.services.trade_execution_service import (
    OrderStatus,
    TradeExecutionService,
    _PendingOrder,
)
from app.core.notifiers import NotifierInterface


class _FakeBroker(BrokerGateway):
    def get_positions(self) -> list[object]:
        return []

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
        *,
        outside_rth: str | None = None,
    ) -> object:
        raise AssertionError("fill-tail lock tests must not submit")

    def get_order_status(self, order_id: str) -> object:
        raise AssertionError("fill-tail lock tests must not query status")


class _RecordingNotifier:
    def __init__(self) -> None:
        self.orders: list[tuple[str, str, str, str, str]] = []

    def notify_order(
        self,
        side: str,
        symbol: str,
        quantity: str,
        price: str,
        order_id: str,
    ) -> bool:
        self.orders.append((side, symbol, quantity, price, order_id))
        return True


def _as_notifier(notifier: _RecordingNotifier) -> NotifierInterface:
    return notifier  # type: ignore[return-value]


def _service(
    *,
    on_fill: Callable[[str, str], None],
    on_reduction_fill: Callable[[str, str, Decimal], None],
) -> TradeExecutionService:
    return TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda *_args: None,
        on_fill=on_fill,
        on_reduction_fill=on_reduction_fill,
    )


def _pending(action: str, order_id: str) -> _PendingOrder:
    return _PendingOrder(
        broker=_FakeBroker(),
        broker_order_id=order_id,
        symbol="TSLA.US",
        action=action,
        quantity=Decimal("1"),
        price=Decimal("250"),
        engine_snapshot=None,
    )


def _status(order_id: str) -> OrderStatus:
    return OrderStatus(
        order_id,
        "FILLED",
        executed_quantity=Decimal("1"),
        executed_price=Decimal("250"),
    )


def _run_abba(
    *,
    action: str,
    order_id: str,
    block_in_reduction: bool,
) -> tuple[TradeExecutionService, list[str], _RecordingNotifier, bool]:
    """Stand-in L is the quote thread's runner lock.

    Quote holds L, then once the fill-tail callback has been entered calls
    ``pending_order_for``. The callback signals entry and then acquires L.
    On the unfixed tail that is the live ABBA cycle.
    """
    stand_in = threading.Lock()
    quote_holds_l = threading.Event()
    callback_entered = threading.Event()
    lock_free = threading.Event()
    calls: list[str] = []
    notifier = _RecordingNotifier()

    def _on_fill(symbol: str, fill_action: str) -> None:
        calls.append(f"fill:{symbol}:{fill_action}")
        if block_in_reduction:
            return
        callback_entered.set()
        stand_in.acquire()
        stand_in.release()

    def _on_reduction(symbol: str, reduction_action: str, quantity: Decimal) -> None:
        calls.append(f"reduction:{symbol}:{reduction_action}:{quantity}")
        if not block_in_reduction:
            return
        callback_entered.set()
        stand_in.acquire()
        stand_in.release()

    tes = _service(on_fill=_on_fill, on_reduction_fill=_on_reduction)
    if action == "SELL":
        tes._record_entry_price("TSLA.US", Decimal("240"), Decimal("1"))

    def _probe() -> None:
        if not callback_entered.wait(timeout=5):
            return
        if not tes._state_lock.acquire(timeout=1):
            return
        tes._state_lock.release()
        lock_free.set()

    def _quote_side() -> None:
        if not stand_in.acquire(timeout=5):
            return
        quote_holds_l.set()
        try:
            if not callback_entered.wait(timeout=5):
                return
            tes.pending_order_for("TSLA.US")
        finally:
            stand_in.release()

    quote = threading.Thread(target=_quote_side, name="quote-stand-in", daemon=True)
    probe = threading.Thread(target=_probe, name="lock-probe", daemon=True)
    finalizer = threading.Thread(
        target=lambda: tes._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
            _pending(action, order_id),
            _status(order_id),
            notifier=_as_notifier(notifier),
        ),
        name="fill-finalizer",
        daemon=True,
    )
    quote.start()
    probe.start()
    assert quote_holds_l.wait(timeout=5), "quote thread did not acquire stand-in lock L"
    finalizer.start()
    finalizer.join(timeout=5)
    quote.join(timeout=5)
    probe.join(timeout=5)
    alive = [thread.name for thread in (finalizer, quote, probe) if thread.is_alive()]
    assert not alive, f"deadlock detected; still alive: {alive}"
    assert lock_free.is_set(), "TES._state_lock still held inside fill-tail callback"
    return tes, calls, notifier, lock_free.is_set()


def test_reduction_sell_fill_tail_does_not_deadlock_with_quote_lock() -> None:
    tes, calls, notifier, lock_free = _run_abba(
        action="SELL",
        order_id="sell-fill-1",
        block_in_reduction=True,
    )
    assert lock_free
    assert calls == ["fill:TSLA.US:SELL", "reduction:TSLA.US:SELL:1"]
    assert notifier.orders == [("SELL", "TSLA.US", "1", "250", "sell-fill-1")]
    assert tes.pending_order_for("TSLA.US") is None


def test_entry_buy_fill_tail_does_not_deadlock_with_quote_lock() -> None:
    _tes, calls, notifier, lock_free = _run_abba(
        action="BUY",
        order_id="buy-fill-1",
        block_in_reduction=False,
    )
    assert lock_free
    assert calls == ["fill:TSLA.US:BUY"]
    assert notifier.orders == [("BUY", "TSLA.US", "1", "250", "buy-fill-1")]


def test_second_finalization_of_the_same_fill_runs_callbacks_once() -> None:
    calls: list[str] = []
    notifier = _RecordingNotifier()

    def _on_fill(symbol: str, action: str) -> None:
        calls.append(f"fill:{symbol}:{action}")

    def _on_reduction(symbol: str, action: str, quantity: Decimal) -> None:
        calls.append(f"reduction:{symbol}:{action}:{quantity}")

    tes = _service(on_fill=_on_fill, on_reduction_fill=_on_reduction)
    tes._record_entry_price("TSLA.US", Decimal("240"), Decimal("1"))
    pending = _pending("SELL", "sell-fill-once")
    status = _status("sell-fill-once")

    tes._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
        pending, status, notifier=_as_notifier(notifier),
    )
    tes._finalize_pending_fill(  # type: ignore[reportPrivateUsage]
        pending, status, notifier=_as_notifier(notifier),
    )

    assert calls == ["fill:TSLA.US:SELL", "reduction:TSLA.US:SELL:1"]
    assert notifier.orders == [("SELL", "TSLA.US", "1", "250", "sell-fill-once")]


def test_pending_skip_does_not_deadlock_with_quote_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """execute() must not call record_order_skipped while holding TES._state_lock.

    The runner's _record_order_skipped takes runner._state_lock via
    _is_primary_symbol. A quote thread already holding that lock then waits
    in pending_order_for. The stand-in is L.
    """
    from app.core.broker import Quote
    from app.core.risk import RiskController
    from app.services import trade_execution_service as trade_module

    monkeypatch.setattr(trade_module, "is_trading_hours", lambda _market, *_args: True)

    stand_in = threading.Lock()
    quote_holds_l = threading.Event()
    skip_entered = threading.Event()
    skipped: list[tuple[str, str, str, str]] = []

    def _record_skipped(
        symbol: str,
        action: str,
        reason: str,
        payload: dict[str, object],
    ) -> None:
        skipped.append((symbol, action, reason, str(payload.get("skip_category"))))
        skip_entered.set()
        stand_in.acquire()
        stand_in.release()

    tes = TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda *_args: None,
        record_order_skipped=_record_skipped,
    )
    tes._pending_order = _pending("SELL", "live-1")  # type: ignore[reportPrivateUsage]

    def _quote_side() -> None:
        if not stand_in.acquire(timeout=5):
            return
        quote_holds_l.set()
        try:
            if not skip_entered.wait(timeout=5):
                return
            tes.pending_order_for("TSLA.US")
        finally:
            stand_in.release()

    quote = threading.Thread(target=_quote_side, name="quote-stand-in", daemon=True)
    executor = threading.Thread(
        target=lambda: tes.execute(
            "SELL",
            "TSLA.US",
            Quote("TSLA.US", 250.0, 249.0, 251.0, "2026-10-07T14:00:00Z"),
            _FakeBroker(),
            RiskController(),
            _as_notifier(_RecordingNotifier()),
            "USD",
        ),
        name="execute-skip",
        daemon=True,
    )
    quote.start()
    assert quote_holds_l.wait(timeout=5), "quote thread did not acquire stand-in lock L"
    executor.start()
    executor.join(timeout=5)
    quote.join(timeout=5)
    alive = [thread.name for thread in (executor, quote) if thread.is_alive()]
    assert not alive, f"deadlock detected; still alive: {alive}"
    assert skipped == [("TSLA.US", "SELL", "pending order in flight", "PENDING")]
