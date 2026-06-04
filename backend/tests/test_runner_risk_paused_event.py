from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock, patch

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
    runner = cast(Any, AppRunner.__new__(AppRunner))
    runner.risk = MagicMock()
    runner.engine = SimpleNamespace(params=SimpleNamespace(market="US"))
    return runner


def _make_live_order(order_id: int, broker_order_id: str, status: str = "SUBMITTED") -> SimpleNamespace:
    return SimpleNamespace(id=order_id, broker_order_id=broker_order_id, status=status)


def test_risk_paused_event_recorded_after_pause() -> None:
    runner = _make_runner()
    db = _FakeDb([_make_live_order(1, "order-live-1")])

    record_event = MagicMock()
    with patch("app.runner.trade_day_for", return_value=date(2026, 6, 4)), patch(
        "app.runner.record_trade_event", new=record_event
    ):
        result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is True
    runner.risk.pause.assert_called_once_with(
        "unresolved live order order-live-1 requires manual confirmation"
    )
    record_event.assert_called_once()
    kwargs = record_event.call_args.kwargs
    assert kwargs["event_type"] == "RISK_PAUSED"
    assert kwargs["status"] == "PAUSED"
    assert kwargs["message"] == "unresolved live order order-live-1 requires manual confirmation"
    assert kwargs["payload"] == {
        "reason": "unresolved_live_order",
        "live_order_id": "order-live-1",
        "trade_day": "2026-06-04",
    }
    assert db.commits == 1


def test_risk_paused_event_payload_complete() -> None:
    runner = _make_runner()
    db = _FakeDb([
        _make_live_order(1, "order-old"),
        _make_live_order(3, "order-new", status="PARTIAL_FILLED"),
    ])

    record_event = MagicMock()
    with patch("app.runner.trade_day_for", return_value=date(2026, 6, 5)), patch(
        "app.runner.record_trade_event", new=record_event
    ):
        runner._pause_if_unresolved_live_order_exists(db)

    detail = record_event.call_args.kwargs["payload"]
    assert set(detail.keys()) == {"reason", "live_order_id", "trade_day"}
    assert detail["live_order_id"] == "order-new"
    assert detail["trade_day"] == "2026-06-05"


def test_record_event_exception_does_not_block_pause() -> None:
    runner = _make_runner()
    db = _FakeDb([_make_live_order(1, "order-live-1")])

    record_event = MagicMock(side_effect=RuntimeError("db error"))
    with patch("app.runner.trade_day_for", return_value=date(2026, 6, 4)), patch(
        "app.runner.record_trade_event", new=record_event
    ):
        result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is True
    runner.risk.pause.assert_called_once_with(
        "unresolved live order order-live-1 requires manual confirmation"
    )
    record_event.assert_called_once()
    assert db.rollbacks == 1
    assert db.commits == 0


def test_no_event_when_no_unsynced_orders() -> None:
    runner = _make_runner()
    db = _FakeDb([])

    record_event = MagicMock()
    with patch("app.runner.record_trade_event", new=record_event):
        result = runner._pause_if_unresolved_live_order_exists(db)

    assert result is False
    runner.risk.pause.assert_not_called()
    record_event.assert_not_called()
    assert db.commits == 0
