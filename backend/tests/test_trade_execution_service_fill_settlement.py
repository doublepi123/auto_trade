from __future__ import annotations

from decimal import Decimal
from dataclasses import replace

import pytest

from app.core.broker import BrokerGateway, OrderResult, OrderStatusResult, Position, Quote
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import trade_execution_service as trade_module
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult, OrderStatus, SettlementConflictError, SettlementIntent,
    SettlementReceipt, TradeExecutionService, _PendingOrder,
)


class _FakeTerminalCallbackStore:
    def __init__(self, complete_failures: int = 0) -> None:
        self.complete_failures = complete_failures
        self.rows: dict[tuple[str, str], str] = {}
        self.releases: list[tuple[str, str]] = []

    def claim(self, broker_order_id: str, terminal_status: str) -> bool:
        key = (broker_order_id, terminal_status)
        if self.rows.get(key) == "APPLIED":
            return False
        self.rows[key] = "PROCESSING"
        return True

    def complete(self, broker_order_id: str, terminal_status: str) -> None:
        if self.complete_failures:
            self.complete_failures -= 1
            raise RuntimeError("database is locked")
        self.rows[(broker_order_id, terminal_status)] = "APPLIED"

    def release(self, broker_order_id: str, terminal_status: str) -> None:
        self.releases.append((broker_order_id, terminal_status))
        self.rows.pop((broker_order_id, terminal_status), None)


class _FakeBroker(BrokerGateway):
    def __init__(self) -> None:
        self.submissions: list[OrderResult] = []

    def get_positions(self) -> list[Position]:
        return [Position("AAPL.US", "LONG", Decimal("50"), Decimal("100"))]

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
        *, outside_rth: str | None = None,
    ) -> OrderResult:
        result = OrderResult("o1", symbol, side, quantity, price, "FILLED")
        self.submissions.append(result)
        return result

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "FILLED", Decimal("50"), Decimal("99"))


class _FakeSettler:
    def __init__(self) -> None:
        self.calls = 0
        self.records: dict[str, SettlementReceipt] = {}

    def __call__(self, intent: SettlementIntent) -> SettlementReceipt:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("accounting transaction aborted")
        stored = self.records.get(intent.broker_order_id)
        if stored is not None:
            return replace(stored, is_new=False)
        receipt = SettlementReceipt(intent, is_new=True)
        self.records[intent.broker_order_id] = receipt
        return receipt


def _service(store: _FakeTerminalCallbackStore) -> TradeExecutionService:
    return TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda *_args: None,
        terminal_callback_store=store,
        final_order_quote_check=lambda _broker, _symbol, _action, price: (
            FinalOrderQuoteCheckResult(executable_price=price)
        ),
    )


def _pending(action: str = "BUY") -> _PendingOrder:
    return _PendingOrder(
        broker=_FakeBroker(), broker_order_id="o1", symbol="AAPL.US", action=action,
        quantity=Decimal("100"), price=Decimal("100"), engine_snapshot=None,
        avg_price=Decimal("100"),
    )


def test_entry_fill_is_booked_once_while_callback_complete_keeps_failing() -> None:
    # Given
    service = _service(_FakeTerminalCallbackStore(3))
    pending = _pending()
    status = OrderStatus("o1", "FILLED", Decimal("100"), Decimal("100"))
    # When
    for _ in range(3):
        with pytest.raises(RuntimeError, match="database is locked"):
            service._finalize_pending_fill(pending, status)
    service._finalize_pending_fill(pending, status)
    # Then
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None
    assert tracked.quantity == Decimal("100")
    assert tracked.cost == Decimal("10000")


def test_partial_sell_loss_reduces_pnl_exactly_once_while_complete_fails() -> None:
    # Given
    service = _service(_FakeTerminalCallbackStore(1))
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    risk = RiskController()
    pending = _pending("SELL")
    status = OrderStatus("o1", "FILLED", Decimal("50"), Decimal("99"))
    # When
    with pytest.raises(RuntimeError, match="database is locked"):
        service._finalize_pending_fill(pending, status, risk=risk)
    service._finalize_pending_fill(pending, status, risk=risk)
    # Then
    assert risk.daily_pnl == -50.0
    assert risk.consecutive_losses == 1
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None
    assert tracked.quantity == Decimal("50")


def test_claim_false_clears_in_flight() -> None:
    # Given
    store = _FakeTerminalCallbackStore()
    store.rows[("o1", "FILLED")] = "APPLIED"
    service = _service(store)
    # When
    service._finalize_pending_fill(_pending(), OrderStatus("o1", "FILLED"))
    # Then
    assert "o1" not in service._fill_finalization_in_flight


def test_fill_path_never_calls_release() -> None:
    # Given
    store = _FakeTerminalCallbackStore(1)
    service = _service(store)
    # When
    with pytest.raises(RuntimeError, match="database is locked"):
        service._finalize_pending_fill(_pending(), OrderStatus("o1", "FILLED"))
    # Then
    assert store.releases == []


def test_same_fill_under_different_terminal_status_has_no_second_effect() -> None:
    service = _service(_FakeTerminalCallbackStore())
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    risk = RiskController()
    status = OrderStatus("o1", "FILLED", Decimal("50"), Decimal("99"))
    service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    service._finalize_pending_fill(_pending("SELL"), replace(status, status="CANCELLED"), risk=risk)
    assert risk.daily_pnl == -50.0
    assert risk.consecutive_losses == 1
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None
    assert tracked.quantity == Decimal("50")


def test_direct_sell_path_shares_settlement_idempotency_with_finalize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_module, "is_trading_hours", lambda _market: True)
    service = _service(_FakeTerminalCallbackStore())
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    broker, risk = _FakeBroker(), RiskController()
    status = service._execute_sell(
        "AAPL.US", Quote("AAPL.US", 99.0, 99.0, 99.0, ""), broker, risk,
        ServerChanNotifier(""), allow_loss_exit=True, reduce_only=True,
    )
    assert status is not None and status.status == "FILLED"
    assert "o1" not in service._finalized_order_ids
    service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    assert risk.daily_pnl == -50.0
    assert risk.consecutive_losses == 1
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None
    assert tracked.quantity == Decimal("50")
    assert len(broker.submissions) == 1


def test_settlement_proceeds_under_halted() -> None:
    service = _service(_FakeTerminalCallbackStore())
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    risk = RiskController()
    risk.kill_switch = True
    service._finalize_pending_fill(
        _pending("SELL"), OrderStatus("o1", "FILLED", Decimal("50"), Decimal("99")), risk=risk,
    )
    assert risk.daily_pnl == -50.0
    assert risk.settlement_consumed("o1")
    assert risk.kill_switch is True
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None and tracked.quantity == Decimal("50")


def test_incompatible_repeat_facts_pause_uncertain_and_never_overwrite() -> None:
    service = _service(_FakeTerminalCallbackStore())
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    risk = RiskController()
    status = OrderStatus("o1", "FILLED", Decimal("50"), Decimal("99"))
    service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    stored = service._settlement_receipts["o1"]
    tracked = service.tracked_position("AAPL.US")
    with pytest.raises(SettlementConflictError, match="quantity differs"):
        service._finalize_pending_fill(
            _pending("SELL"), replace(status, executed_quantity=Decimal("60")), risk=risk,
        )
    assert risk.paused is True
    assert "UNCERTAIN" in risk.pause_reason
    assert service._settlement_receipts["o1"] == stored
    assert service.tracked_position("AAPL.US") == tracked
    assert risk.daily_pnl == -50.0


def test_accounting_failure_before_commit_books_nothing_then_exactly_once() -> None:
    settler = _FakeSettler()
    service = TradeExecutionService(
        record_order=lambda *_args: None, update_order_status=lambda *_args: None,
        record_risk_event=lambda *_args: None, settle_fill=settler,
    )
    service._record_entry_price("AAPL.US", Decimal("100"), Decimal("100"))
    before = service.tracked_position("AAPL.US")
    risk = RiskController()
    status = OrderStatus("o1", "FILLED", Decimal("50"), Decimal("99"))
    with pytest.raises(RuntimeError, match="accounting transaction aborted"):
        service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    assert settler.records == {}
    assert service.tracked_position("AAPL.US") == before
    assert risk.daily_pnl == 0.0
    assert not risk.settlement_consumed("o1")
    service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    service._finalize_pending_fill(_pending("SELL"), status, risk=risk)
    assert len(settler.records) == 1
    assert risk.daily_pnl == -50.0
    assert risk.consecutive_losses == 1
    tracked = service.tracked_position("AAPL.US")
    assert tracked is not None and tracked.quantity == Decimal("50")


def test_exits_still_submit_while_settlement_tail_is_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_module, "is_trading_hours", lambda _market: True)
    service = _service(_FakeTerminalCallbackStore(-1))
    risk = RiskController()
    entry = replace(_pending(), broker_order_id="entry")
    for _ in range(3):
        with pytest.raises(RuntimeError, match="database is locked"):
            service._finalize_pending_fill(
                entry, OrderStatus("entry", "FILLED", Decimal("100"), Decimal("100")), risk=risk,
            )
    broker = _FakeBroker()
    status = service._execute_sell(
        "AAPL.US", Quote("AAPL.US", 99.0, 99.0, 99.0, ""), broker, risk,
        ServerChanNotifier(""), allow_loss_exit=True, reduce_only=True,
    )
    assert status is not None and status.status == "FILLED"
    assert len(broker.submissions) == 1
    assert broker.submissions[0].side == "SELL"
    assert risk.paused is False
