from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

from app.runner import AppRunner


class _FakeQuery:
    def __init__(self, orders: list[SimpleNamespace]) -> None:
        self._orders = list(orders)

    def filter(self, *_args, **_kwargs) -> _FakeQuery:
        return self

    def order_by(self, *_args, **_kwargs) -> _FakeQuery:
        self._orders.sort(key=lambda order: getattr(order, "id", 0), reverse=True)
        return self

    def first(self) -> SimpleNamespace | None:
        return self._orders[0] if self._orders else None

    def all(self) -> list[SimpleNamespace]:
        return list(self._orders)


class _FakeDb:
    def __init__(self, orders: list[SimpleNamespace]) -> None:
        self._orders = orders
        self.commits = 0
        self.rollbacks = 0

    def query(self, _model: object) -> _FakeQuery:
        return _FakeQuery(self._orders)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1


def _make_runner() -> Any:
    runner = cast(Any, AppRunner())
    runner.engine.params.symbol = "AAPL.US"
    runner.risk = MagicMock()
    runner.risk.paused = False
    runner.risk.pause_reason = ""
    runner._persist_risk_pause_best_effort = MagicMock()
    runner._record_risk_event = MagicMock()
    runner._broadcast_status = MagicMock()
    return runner


def _make_live_order(order_id: int, broker_order_id: str, status: str = "SUBMITTED") -> SimpleNamespace:
    return SimpleNamespace(
        id=order_id,
        broker_order_id=broker_order_id,
        symbol="AAPL.US",
        quantity=1,
        price=100,
        status=status,
    )


def test_risk_paused_event_recorded_after_pause() -> None:
    runner = _make_runner()
    db = _FakeDb([_make_live_order(1, "order-live-1")])

    result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is True
    reason = runner.risk.pause.call_args.args[0]
    assert reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    assert "AAPL.US=[order-live-1]" in reason
    runner.risk.pause.assert_called_once_with(reason, auto_resumable=False)
    runner._record_risk_event.assert_called_once_with(reason)
    runner._persist_risk_pause_best_effort.assert_called_once_with()
    runner._broadcast_status.assert_called_once_with()
    assert runner._unresolved_live_order_ids == ["order-live-1"]


def test_risk_pause_keeps_complete_live_order_inventory() -> None:
    runner = _make_runner()
    db = _FakeDb([
        _make_live_order(1, "order-old"),
        _make_live_order(3, "order-new", status="PARTIAL_FILLED"),
    ])

    assert runner._pause_if_unresolved_live_order_exists(db) is True

    reason = runner.risk.pause.call_args.args[0]
    assert "order-old" in reason
    assert "order-new" in reason
    assert runner._unresolved_live_order_ids == ["order-new", "order-old"]
    runner._record_risk_event.assert_called_once_with(reason)


def test_record_event_exception_does_not_block_pause() -> None:
    runner = _make_runner()
    db = _FakeDb([_make_live_order(1, "order-live-1")])

    runner._record_risk_event.side_effect = RuntimeError("db error")
    result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is True
    reason = runner.risk.pause.call_args.args[0]
    runner.risk.pause.assert_called_once_with(reason, auto_resumable=False)
    runner._record_risk_event.assert_called_once_with(reason)
    runner._persist_risk_pause_best_effort.assert_called_once_with()
    runner._broadcast_status.assert_called_once_with()


def test_no_event_when_no_unsynced_orders() -> None:
    runner = _make_runner()
    db = _FakeDb([])

    result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is False
    runner.risk.pause.assert_not_called()
    runner._record_risk_event.assert_not_called()
    runner._persist_risk_pause_best_effort.assert_not_called()
    runner._broadcast_status.assert_not_called()
