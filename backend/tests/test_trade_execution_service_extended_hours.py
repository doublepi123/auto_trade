from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.broker import (
    BrokerGateway, ExtendedHoursUnsupportedError, OrderResult, OrderStatusResult,
    Position, Quote,
)
from app.core.engine import EngineSnapshot, EngineState
from app.core.execution_session import ExecutionSessionDecision
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import trade_execution_service as execution


class _FakeBroker(BrokerGateway):
    def __init__(self) -> None:
        self.submissions: list[tuple[str, str, Decimal, Decimal, str | None]] = []
        self.cancelled: list[str] = []
        self.submit_status = "SUBMITTED"
        self.unsupported = False
        self.polled = OrderStatusResult("exit-1", "SUBMITTED", outside_rth="ANY_TIME")
        self.cancelled_status = OrderStatusResult("exit-1", "CANCELLED")

    def get_positions(self) -> list[Position]:
        return [Position("TSLA.US", "LONG", Decimal("10"), Decimal("100"))]

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
        *, outside_rth: str | None = None,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price, outside_rth))
        if self.unsupported:
            raise ExtendedHoursUnsupportedError("SDK does not support outside_rth")
        return OrderResult("exit-1", symbol, side, quantity, price, self.submit_status)

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return self.polled

    def cancel_order(self, order_id: str) -> OrderStatusResult:
        self.cancelled.append(order_id)
        return self.cancelled_status


def _session(phase: str = "POST") -> ExecutionSessionDecision:
    if phase == "POST":
        return ExecutionSessionDecision("US", "POST", "supported post-market", None, None)
    return ExecutionSessionDecision("US", "UNAVAILABLE", "overnight unavailable", None, None)


class _Harness:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(execution, "is_trading_hours", lambda _market: False)
        monkeypatch.setattr(execution, "resolve_execution_session", lambda *args: _session())
        self.broker = _FakeBroker()
        self.risk = RiskController()
        self.restored: list[EngineSnapshot] = []
        self.events: list[str] = []
        self.skips: list[dict[str, object]] = []
        self.fills: list[Decimal] = []
        self.snapshot = EngineSnapshot(EngineState.LONG, 100.0, None)
        self.service = execution.TradeExecutionService(
            record_order=lambda *_args: None,
            update_order_status=lambda *_args: None,
            record_risk_event=self.events.append,
            record_order_skipped=lambda _symbol, _action, _reason, payload: self.skips.append(payload),
            on_reduction_fill=lambda _symbol, _action, qty: self.fills.append(qty),
            final_order_quote_check=lambda *_args: execution.FinalOrderQuoteCheckResult(
                executable_price=Decimal("99"), bid=Decimal("99"), ask=Decimal("100"),
                price_floor=Decimal("99.50"),
            ),
            extended_hours_protective_exits_enabled=True,
            paper_account_confirmed=False,
        )
        self.service.load_tracked_entries({"TSLA.US": (Decimal("10"), Decimal("1000"))})

    def execute(self, action: str = "SELL", *, reduce_only: bool = True) -> execution.OrderStatus:
        result = self.service.execute(
            action, "TSLA.US", Quote("TSLA.US", 100, 99, 100, ""), self.broker,
            self.risk, ServerChanNotifier(""), "USD", trading_session_mode="RTH_ONLY",
            allow_loss_exit=True, reduce_only=reduce_only, engine_snapshot=self.snapshot,
            restore_engine_snapshot=self.restored.append,
        )
        assert result is not None
        return result

    def reconcile(self, *, timeout: bool = True) -> None:
        pending = self.service.pending_order_for("TSLA.US")
        assert pending is not None
        pending = replace(pending, next_status_check_at=0, submitted_at=0 if timeout else pending.submitted_at)
        self.service._reconcile_pending_order(pending, risk=self.risk, notifier=ServerChanNotifier(""))


def test_post_market_reduce_only_stop_reaches_boundary_with_any_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    h = _Harness(monkeypatch)
    calls: list[str] = []
    boundary = h.service.pre_submit_risk_check

    def observe(request: execution._PreSubmitRiskRequest, broker: BrokerGateway) -> execution.ApprovedOrder | execution.OrderStatus:
        calls.append(request.action)
        return boundary(request, broker)

    monkeypatch.setattr(h.service, "pre_submit_risk_check", observe)
    # When
    result = h.execute()
    # Then
    assert result.status == "SUBMITTED"
    assert calls == ["SELL"]
    assert h.broker.submissions == [("TSLA.US", "SELL", Decimal("10"), Decimal("99.50"), "ANY_TIME")]


@pytest.mark.parametrize("action", ["BUY", "SELL_SHORT"])
def test_post_market_entry_is_still_session_rejected(monkeypatch: pytest.MonkeyPatch, action: str) -> None:
    # Given
    h = _Harness(monkeypatch)
    h.service.short_entries_enabled = True
    # When
    result = h.execute(action, reduce_only=False)
    # Then
    assert result.status == "SKIPPED"
    assert h.skips[-1]["skip_category"] == "SESSION"
    assert h.broker.submissions == []


def test_sell_without_reduce_only_is_not_widened(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    result = h.execute(reduce_only=False)
    assert result.status == "SKIPPED"
    assert h.skips[-1]["skip_category"] == "SESSION"
    assert h.broker.submissions == []


def test_paper_account_keeps_intent_zero_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.service.paper_account_confirmed = True
    result = h.execute()
    assert "paper account" in result.reason
    assert h.broker.submissions == []
    position = h.service.tracked_position("TSLA.US")
    assert position is not None and position.quantity == Decimal("10")


def test_unavailable_phase_keeps_intent_zero_orders(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    monkeypatch.setattr(execution, "resolve_execution_session", lambda *args: _session("UNAVAILABLE"))
    result = h.execute()
    assert result.status == "SKIPPED"
    assert h.broker.submissions == []


def test_final_binding_rejects_when_session_closes_before_submit(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    phases = iter([_session(), _session("UNAVAILABLE")])
    monkeypatch.setattr(execution, "resolve_execution_session", lambda *args: next(phases))
    result = h.execute()
    assert "closed before submission" in result.reason
    assert h.broker.submissions == []


def test_final_binding_uses_plain_order_if_rth_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    rth = iter([False, True])
    monkeypatch.setattr(execution, "is_trading_hours", lambda _market: next(rth))
    result = h.execute()
    assert result.status == "SUBMITTED"
    assert h.broker.submissions[0][-1] is None


def test_halted_still_rejects_extended_hours_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.risk.enable_kill_switch()
    result = h.execute()
    assert result.status == "SKIPPED"
    assert h.skips[-1]["skip_category"] == "RISK"
    assert h.broker.submissions == []


@pytest.mark.parametrize("floor", [Decimal("99.50"), None])
def test_floor_clamp_applies_to_extended_hours_price(monkeypatch: pytest.MonkeyPatch, floor: Decimal | None) -> None:
    h = _Harness(monkeypatch)
    h.service._final_order_quote_check = lambda *_args: execution.FinalOrderQuoteCheckResult(
        executable_price=Decimal("99"), bid=Decimal("99"), ask=Decimal("100"), price_floor=floor,
    )
    result = h.execute()
    assert result.status == "SUBMITTED"
    assert h.broker.submissions[0][3] == Decimal("99.50")


def test_sdk_unsupported_skips_without_pause_and_latches(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    monkeypatch.setattr(execution.TradeExecutionService, "_extended_hours_sdk_unsupported", False)
    h.broker.unsupported = True
    result = h.execute()
    assert result.status == "SKIPPED"
    assert h.skips[-1]["skip_category"] == "RISK"
    assert h.risk.paused is False
    h.execute()
    assert len(h.broker.submissions) == 1
    other = _Harness(monkeypatch)
    assert other.execute().status == "SKIPPED"
    assert other.broker.submissions == []


def test_rejected_extended_hours_order_does_not_pause_and_stops_repeating(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.broker.submit_status = "REJECTED"
    result = h.execute()
    assert result.status == "REJECTED"
    assert h.risk.paused is False
    assert h.events
    assert h.restored == [h.snapshot]
    h.execute()
    assert len(h.broker.submissions) == 1
    monkeypatch.setattr(execution, "is_trading_hours", lambda _market: True)
    h.broker.submit_status = "SUBMITTED"
    assert h.execute().status == "SUBMITTED"
    assert h.broker.submissions[-1][-1] is None


def test_timeout_cancel_confirmed_zero_fill_keeps_intent_no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    assert h.execute().status == "SUBMITTED"
    h.reconcile()
    assert h.risk.paused is False
    assert h.service.pending_order_for("TSLA.US") is None
    assert h.restored == [h.snapshot]
    position = h.service.tracked_position("TSLA.US")
    assert position is not None and position.quantity == Decimal("10")
    assert h.execute().status == "SKIPPED"
    assert len(h.broker.submissions) == 1


def test_timeout_cancel_partial_fill_finalizes_actual_quantity(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.broker.cancelled_status = OrderStatusResult("exit-1", "CANCELLED", Decimal("3"), Decimal("99.5"))
    assert h.execute().status == "SUBMITTED"
    h.reconcile()
    assert h.fills == [Decimal("3")]
    assert h.risk.paused is False
    position = h.service.tracked_position("TSLA.US")
    assert position is not None and position.quantity == Decimal("7")


def test_cancel_accepted_unknown_status_keeps_pending_and_never_resubmits(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.broker.cancelled_status = OrderStatusResult("exit-1", "CANCEL_REQUESTED")
    assert h.execute().status == "SUBMITTED"
    h.reconcile()
    assert h.risk.paused is True
    assert h.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    assert h.service.pending_order_for("TSLA.US") is not None
    h.execute()
    assert len(h.broker.submissions) == 1


@pytest.mark.parametrize("rebuild", ["reconcile", "merge", "refresh", "load"])
def test_pending_rebuild_preserves_extended_hours_flag(monkeypatch: pytest.MonkeyPatch, rebuild: str) -> None:
    h = _Harness(monkeypatch)
    assert h.execute().status == "SUBMITTED"
    initial = h.service.pending_order_for("TSLA.US")
    assert initial is not None
    if rebuild == "reconcile":
        h.reconcile(timeout=False)
    elif rebuild == "merge":
        h.service._track_pending_order("SELL", OrderResult("exit-1", "TSLA.US", "SELL", Decimal("10"), Decimal("99.5"), "SUBMITTED"), h.broker, None)
    elif rebuild == "refresh":
        h.service.refresh_pending_brokers(h.broker)
    else:
        h.service.load_pending_orders([replace(initial, extended_hours=False, extended_hours_key=None)])
    pending = h.service.pending_order_for("TSLA.US")
    assert pending is not None
    assert pending.extended_hours is True
    assert pending.extended_hours_key == initial.extended_hours_key


def test_server_override_cancels_once_and_latches_without_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    h.broker.polled = OrderStatusResult("exit-1", "SUBMITTED", outside_rth="RTH_ONLY")
    assert h.execute().status == "SUBMITTED"
    h.reconcile(timeout=False)
    assert h.broker.cancelled == ["exit-1"]
    assert h.risk.paused is False
    assert h.service.pending_order_for("TSLA.US") is None
    h.execute()
    assert len(h.broker.submissions) == 1


def test_retry_backoff_cap_is_phase_and_day_scoped(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    for attempt in range(3):
        monkeypatch.setattr(execution.time, "monotonic", lambda: 100.0 + attempt * 61)
        assert h.execute().status == "SUBMITTED"
        h.reconcile()
        assert h.execute().status == "SKIPPED"
    monkeypatch.setattr(execution.time, "monotonic", lambda: 1000.0)
    assert h.execute().status == "SKIPPED"
    assert len(h.broker.submissions) == 3
    assert h.risk.paused is False
    now = datetime.now(timezone.utc)
    tomorrow = now + timedelta(days=1)
    assert h.service.extended_hours_exit_decision(action="SELL", symbol="TSLA.US", market="US", reduce_only=True, instant=tomorrow).permitted
    monkeypatch.setattr(execution, "resolve_execution_session", lambda *args: ExecutionSessionDecision("US", "PRE", "supported pre-market", None, None))
    assert h.service.extended_hours_exit_decision(action="SELL", symbol="TSLA.US", market="US", reduce_only=True, instant=now).permitted


def test_rth_exit_never_consults_extended_hours_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    monkeypatch.setattr(execution, "is_trading_hours", lambda _market: True)

    def unexpected_resolver(market: str, instant: datetime | None = None) -> ExecutionSessionDecision:
        pytest.fail("RTH orders must not resolve extended hours")

    monkeypatch.setattr(execution, "resolve_execution_session", unexpected_resolver)
    assert h.execute().status == "SUBMITTED"
    assert h.broker.submissions[0][-1] is None


def test_default_opt_in_disabled_refuses_extended_hours(monkeypatch: pytest.MonkeyPatch) -> None:
    h = _Harness(monkeypatch)
    service = execution.TradeExecutionService(lambda *_args: None, lambda *_args: None, lambda *_args: None)
    decision = service.extended_hours_exit_decision(action="SELL", symbol="TSLA.US", market="US", reduce_only=True)
    assert not decision.permitted
    assert "disabled" in decision.reason
    assert h.broker.submissions == []


@pytest.mark.parametrize("override", ["", "RTH_ONLY", "OVERNIGHT"])
def test_override_cancel_accepted_retains_pending_and_cancels_only_once(monkeypatch: pytest.MonkeyPatch, override: str) -> None:
    h = _Harness(monkeypatch)
    h.broker.polled = OrderStatusResult("exit-1", "SUBMITTED", outside_rth=override)
    h.broker.cancelled_status = OrderStatusResult("exit-1", "CANCEL_REQUESTED")
    assert h.execute().status == "SUBMITTED"
    h.reconcile(timeout=False)
    h.reconcile(timeout=False)
    assert h.broker.cancelled == ["exit-1"]
    assert h.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
    assert h.service.pending_order_for("TSLA.US") is not None
    assert h.execute().status == "SKIPPED"
    assert len(h.broker.submissions) == 1
