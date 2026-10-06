# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
"""P3b review 1: entry and exit extended-hours budgets are separate,
and an ANY take-profit SELL may submit in an executable POST phase.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from app.core.broker import OrderResult, OrderStatusResult, Position, Quote
from app.core.engine import EngineState, StrategyParams
from app.core.notifiers.serverchan import ServerChanNotifier
from app.core.risk import RiskController
from app.runner import AppRunner
from app.services import trade_execution_service as execution
from app.services.trade_execution_service import TradeExecutionService
from tests.test_runner_degraded_exit import _execution_sandbox, _quote, _runner
from tests.test_runner_extended_hours_exit import _session


class _RejectBroker:
    def __init__(self) -> None:
        self.submissions = 0

    def submit_limit_order(self, *args, **kwargs) -> OrderResult:
        self.submissions += 1
        return OrderResult("rej", "TSLA.US", "SELL", Decimal("1"), Decimal("100"), "REJECTED")

    def get_order_status(self, order_id: str) -> OrderStatusResult:
        return OrderStatusResult(order_id, "REJECTED")


def test_one_exit_rejection_does_not_disable_entries_or_the_next_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    svc = TradeExecutionService(
        record_order=lambda *_a: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
    )
    svc.extended_hours_trading_enabled = True
    now = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(execution.time, "monotonic", lambda: 1_000.0)
    svc._extended_hours_context = ("TSLA.US", "POST", now)
    broker = _RejectBroker()
    pending_key = svc._active_extended_hours_key()
    assert pending_key is not None and pending_key[-1] == "EXIT"
    svc._extended_hours_terminal_outcome(pending_key, unsupported=False)
    monkeypatch.setattr(execution.time, "monotonic", lambda: 1_061.0)

    entry = svc._extended_hours_entry_decision(
        symbol="TSLA.US", market="US", instant=now,
    )
    exit_decision = svc.extended_hours_exit_decision(
        action="SELL", symbol="TSLA.US", market="US", reduce_only=True, instant=now,
    )

    assert entry is not None and entry.permitted
    assert exit_decision.permitted


def test_three_exit_failures_disable_exits_with_risk_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    svc = TradeExecutionService(
        record_order=lambda *_a: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=events.append,
    )
    svc.extended_hours_trading_enabled = True
    now = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(execution.time, "monotonic", lambda: 1_000.0)
    for _ in range(3):
        svc._extended_hours_context = ("TSLA.US", "POST", now)
        key = svc._active_extended_hours_key()
        assert key is not None
        svc._extended_hours_terminal_outcome(key, unsupported=False)

    exit_decision = svc.extended_hours_exit_decision(
        action="SELL", symbol="TSLA.US", market="US", reduce_only=True, instant=now,
    )
    entry = svc._extended_hours_entry_decision(
        symbol="TSLA.US", market="US", instant=now,
    )

    assert not exit_decision.permitted
    assert "exit" in exit_decision.reason.lower()
    assert any("exit" in event.lower() for event in events)
    assert entry is not None and entry.permitted


def test_unsupported_signal_disables_both_kinds() -> None:
    svc = TradeExecutionService(
        record_order=lambda *_a: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
    )
    svc.extended_hours_trading_enabled = True
    now = datetime(2026, 10, 6, 21, 0, tzinfo=timezone.utc)
    svc._extended_hours_context = ("TSLA.US", "POST", now)
    key = svc._active_extended_hours_key()
    assert key is not None
    svc._extended_hours_terminal_outcome(key, unsupported=True)

    exit_decision = svc.extended_hours_exit_decision(
        action="SELL", symbol="TSLA.US", market="US", reduce_only=True, instant=now,
    )
    entry = svc._extended_hours_entry_decision(
        symbol="TSLA.US", market="US", instant=now,
    )

    assert not exit_decision.permitted
    assert entry is not None and not entry.permitted


def test_take_profit_sell_in_post_any_submits_any_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "POST")
    runner = _runner(monkeypatch)
    runner.engine.params = StrategyParams(
        symbol="NVDA.US", market="US", buy_low=90, sell_high=100,
    )
    runner.engine.state = EngineState.LONG
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    quote = _quote(100.5, 100.4, 100.6)
    runner._remember_quote(quote)
    broker_orders: list[tuple] = []

    class _Broker:
        def get_positions(self):
            return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

        def get_quotes(self, symbols):
            return [quote]

        def estimate_margin_max_quantity(self, *args, **kwargs):
            return Decimal("0")

        def submit_limit_order(self, symbol, side, quantity, price, *, outside_rth=None):
            broker_orders.append((symbol, side, quantity, price, outside_rth))
            return OrderResult("tp", symbol, side, quantity, price, "SUBMITTED")

        def get_order_status(self, order_id):
            return OrderStatusResult(order_id, "SUBMITTED")

    with _execution_sandbox(runner, monkeypatch, quote):
        runner.broker = _Broker()
        decision = runner._evaluate_quote_trigger(quote)
        runner._execute_triggered_order(decision, quote)

    assert broker_orders
    assert broker_orders[0][1] == "SELL"
    assert broker_orders[0][2] == Decimal("5")
    assert broker_orders[0][4] == "ANY_TIME"


def test_post_price_stop_submits_with_fresh_quote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "POST")
    runner = _runner(monkeypatch)
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    quote = _quote(98.9, 98.8, 99.0)
    runner._remember_quote(quote)
    orders: list[tuple] = []

    class _Broker:
        def get_positions(self):
            return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

        def get_quotes(self, symbols):
            return [quote]

        def submit_limit_order(self, symbol, side, quantity, price, *, outside_rth=None):
            orders.append((symbol, side, quantity, outside_rth))
            return OrderResult("stop", symbol, side, quantity, price, "SUBMITTED")

        def get_order_status(self, order_id):
            return OrderStatusResult(order_id, "SUBMITTED")

    with _execution_sandbox(runner, monkeypatch, quote):
        runner.broker = _Broker()
        runner._on_quote(quote)

    assert orders
    assert orders[0][1] == "SELL"
    assert orders[0][3] == "ANY_TIME"


def test_post_bid_below_floor_is_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "POST")
    runner = _runner(monkeypatch)
    runner._trading_session_mode = "ANY"
    runner._trade_svc.extended_hours_trading_enabled = True
    trusted = _quote(100.0, 99.9, 100.1)
    runner._remember_quote(trusted)
    crashed = _quote(90.0, 80.0, 80.2)
    orders: list[object] = []

    class _Broker:
        def get_positions(self):
            return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

        def get_quotes(self, symbols):
            return [crashed]

        def submit_limit_order(self, *args, **kwargs):
            orders.append(args)
            raise AssertionError("bid below floor must not submit")

    with _execution_sandbox(runner, monkeypatch, crashed):
        runner.broker = _Broker()
        runner._on_quote(crashed)

    assert orders == []
    assert runner.last_action_message


def test_rth_sell_is_not_capped_by_tracked_quantity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.broker import OrderResult, OrderStatusResult, Position
    from app.services.trade_execution_service import TradeExecutionService
    from tests.test_trade_execution_service_extended_hours_trading import _pin_clock

    instant = datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc)
    _pin_clock(monkeypatch, instant)
    submitted: list[Decimal] = []

    class _Broker:
        def get_positions(self):
            return [Position("TSLA.US", "LONG", Decimal("8"), Decimal("100"))]

        def submit_limit_order(self, symbol, side, quantity, price, *, outside_rth=None):
            submitted.append(quantity)
            return OrderResult("rth", symbol, side, quantity, price, "SUBMITTED")

        def get_order_status(self, order_id):
            return OrderStatusResult(order_id, "SUBMITTED")

    svc = TradeExecutionService(
        record_order=lambda *_a, **_k: None,
        update_order_status=lambda *_a, **_k: True,
        record_risk_event=lambda *_a, **_k: None,
        final_order_quote_check=lambda *_a: execution.FinalOrderQuoteCheckResult(
            executable_price=Decimal("110"),
        ),
    )
    svc.load_tracked_entries({
        "TSLA.US": (Decimal("3"), Decimal("300"), "LONG", instant),
    })

    status = svc.execute(
        "SELL",
        "TSLA.US",
        Quote("TSLA.US", 110, 109.9, 110.1, instant.isoformat()),
        _Broker(),
        RiskController(),
        ServerChanNotifier(""),
        "USD",
        market="US",
    )

    assert status is not None and status.status == "SUBMITTED"
    # Broker availability is 8 and the tracked LONG is 3. The extended
    # take-profit cap must not apply to an ordinary RTH SELL; the
    # pre-existing availability check may still bound the submitted size,
    # but it must not be the new tracked-quantity cap of 3.
    assert submitted and submitted[0] != Decimal("3")


def test_take_profit_sell_rth_only_still_refused_in_post(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _session(monkeypatch, "POST")
    runner = _runner(monkeypatch)
    runner.engine.params = StrategyParams(
        symbol="NVDA.US", market="US", buy_low=90, sell_high=100,
    )
    runner._trading_session_mode = "RTH_ONLY"
    runner._trade_svc.extended_hours_trading_enabled = True
    quote = _quote(100.5, 100.4, 100.6)
    runner._remember_quote(quote)
    orders: list[object] = []

    class _Broker:
        def get_positions(self):
            return [Position("NVDA.US", "LONG", Decimal("5"), Decimal("100"))]

        def get_quotes(self, symbols):
            return [quote]

        def submit_limit_order(self, *args, **kwargs):
            orders.append(args)
            return OrderResult("no", "NVDA.US", "SELL", Decimal("5"), Decimal("100"), "SUBMITTED")

    with _execution_sandbox(runner, monkeypatch, quote):
        runner.broker = _Broker()
        decision = runner._evaluate_quote_trigger(quote)
        if decision.result is not None:
            runner._execute_triggered_order(decision, quote)

    assert orders == []
