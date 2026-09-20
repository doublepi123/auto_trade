from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app import runner as runner_module
from app.core.broker import OrderResult
from app.core.engine import EngineState
from app.core.execution_session import ExecutionPhase, ExecutionSessionDecision
from app.core.log_throttle import RepeatedLogThrottle
from app.models import RuntimeState
from app.runner import AppRunner
from app.services import trade_execution_service as execution_module
from app.services.trade_execution_service import FinalOrderQuoteCheckResult
from tests.test_runner_degraded_exit import (
    _FakeBroker, _execution_sandbox, _floor, _latch, _quote, _runner,
)


class _FakeExtendedBroker(_FakeBroker):
    def __init__(self) -> None:
        super().__init__(_quote(98.9, 98.8, 99))
        self.orders: list[tuple[str, str, Decimal, Decimal, str | None]] = []

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
        *, outside_rth: str | None = None,
    ) -> OrderResult:
        self.orders.append((symbol, side, quantity, price, outside_rth))
        return OrderResult("extended-exit", symbol, side, quantity, price, "SUBMITTED")


def _session(
    monkeypatch: pytest.MonkeyPatch, phase: ExecutionPhase = "POST",
) -> ExecutionSessionDecision:
    now = datetime.now(timezone.utc)
    session = ExecutionSessionDecision(
        "US", phase, "overnight" if phase == "UNAVAILABLE" else "supported execution window",
        now - timedelta(seconds=5), now + timedelta(hours=1),
    )
    for module in (runner_module, execution_module):
        monkeypatch.setattr(module, "is_trading_hours", lambda _: phase == "RTH")
        monkeypatch.setattr(module, "resolve_execution_session", lambda *a: session, raising=False)
    return session


def _extended_runner(monkeypatch: pytest.MonkeyPatch) -> AppRunner:
    runner = _runner(monkeypatch)
    monkeypatch.setattr(runner, "_get_trading_session_mode", lambda: "RTH_ONLY")
    runner._trade_svc.extended_hours_protective_exits_enabled = True
    runner._trade_svc.paper_account_confirmed = False
    return runner


def test_post_market_latched_stop_produces_trigger_and_submits_any_time(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a persisted protective intent and a supported post-market session.
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    intent = _latch(runner)
    broker = _FakeExtendedBroker()
    with _execution_sandbox(runner, monkeypatch, broker.quote):
        monkeypatch.setattr(runner, "broker", broker)
        assert runner._persist_reduction(intent, "NVDA.US")
        # When the quote is evaluated and the resulting decision is executed.
        decision = runner._evaluate_quote_trigger(broker.quote)
        runner._execute_triggered_order(decision, broker.quote)
        # Then one floor-protected extended-hours order crosses the real service.
        assert broker.orders == [("NVDA.US", "SELL", Decimal("5"), Decimal("98.80"), "ANY_TIME")], runner.decision_funnel.snapshot()
        assert decision.result is not None and decision.reduce_only and decision.allow_loss_exit
        assert decision.execution_phase == "POST"
        assert broker.orders[0][3] >= Decimal(str(runner._reduce_only_price_floors["NVDA.US"].price))
        assert "NVDA.US" in runner._reduction_intents
        assert runner._last_trusted_push_quote_at == 0
        assert runner._last_quote_at == 0


def test_post_market_entry_never_triggers(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a flat engine below its entry threshold outside RTH.
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    runner._trade_svc.load_tracked_entries({})
    runner.engine.state = EngineState.FLAT
    broker = _FakeExtendedBroker()
    with _execution_sandbox(runner, monkeypatch, broker.quote):
        monkeypatch.setattr(runner, "broker", broker)
        # When an entry-price quote arrives.
        decision = runner._evaluate_quote_trigger(broker.quote)
        runner._execute_triggered_order(decision, broker.quote)
        # Then no entry is triggered or submitted.
        assert decision.result is None
        assert broker.orders == []
        assert runner.engine.state == EngineState.FLAT


def test_overnight_keeps_intent_zero_orders_no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an unavailable execution session and a newly due time stop.
    _session(monkeypatch, "UNAVAILABLE")
    runner = _extended_runner(monkeypatch)
    runner.engine.params.max_holding_minutes = 1
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        # When the callback receives a fresh quote.
        runner._on_quote(_quote())
        # Then the intent is durable and waiting, without pausing or ordering.
        assert runner.decision_funnel.snapshot().skips_by_category["SESSION"] == 1
        assert "NVDA.US" in runner._reduction_intents
        assert runner.risk.paused is False
        assert broker.submitted == []
        with runner_module.SessionLocal() as db:
            state = db.scalar(select(RuntimeState).where(RuntimeState.symbol == "NVDA.US"))
            assert state is not None and state.reduction_cause == "TIME_STOP"


def test_paper_account_waits_with_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given an otherwise permitted paper-account exit.
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    runner._trade_svc.paper_account_confirmed = True
    _latch(runner)
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        # When the quote callback runs.
        runner._on_quote(_quote())
        # Then the shared policy's reason is visible and no order is sent.
        assert "paper account does not support extended hours" in runner.last_action_message
        assert broker.submitted == []
        assert "NVDA.US" in runner._reduction_intents


def test_waiting_message_is_throttled(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    # Given a fixed throttle clock and an unavailable session.
    _session(monkeypatch, "UNAVAILABLE")
    runner = _extended_runner(monkeypatch)
    _latch(runner)
    throttle = RepeatedLogThrottle(window_seconds=60, clock=lambda: 100)
    monkeypatch.setattr(runner, "_extended_hours_exit_log_throttle", throttle, raising=False)
    # When twenty consecutive refused quotes arrive.
    for _ in range(20):
        runner._evaluate_quote_trigger(_quote())
    # Then one warning is emitted and the rest are counted, preserving intent.
    warnings = [record for record in caplog.records if "protective exit waiting" in record.message]
    assert len(warnings) == 1
    assert throttle.suppressed_count == 19
    assert "overnight" in runner.last_action_message
    assert "NVDA.US" in runner._reduction_intents


@pytest.mark.parametrize("expired", [False, True])
def test_extended_hours_final_check_requires_registered_floor(monkeypatch: pytest.MonkeyPatch, expired: bool) -> None:
    # Given a healthy quote but missing or expired floor evidence.
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    if expired:
        _floor(runner, age=301)
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        # When the final quote boundary runs.
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        # Then healthy BBO does not bypass the required floor.
        assert result == "extended-hours exit requires a registered price floor"
        assert broker.submitted == []


def test_extended_hours_final_check_rejects_quote_predating_session(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given a fresh cached quote just before the current phase began.
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    _floor(runner)
    with _execution_sandbox(runner, monkeypatch, _quote(age=10)) as (broker, _):
        # When the final quote boundary runs.
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        # Then a previous-phase BBO is rejected even while still fresh.
        assert result == "executable quote predates the current execution session"
        assert broker.submitted == []


def test_rth_final_check_unchanged_without_floor(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given RTH and no registered floor.
    _session(monkeypatch, "RTH")
    runner = _extended_runner(monkeypatch)
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        # When the legacy final gate checks a healthy quote.
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        # Then the legacy executable price is accepted without a floor.
        assert isinstance(result, FinalOrderQuoteCheckResult)
        assert result.executable_price == Decimal("100.45") and result.price_floor is None
        assert broker.submitted == []


def test_reduce_only_final_check_unaffected_when_extended_hours_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    runner._trade_svc.extended_hours_protective_exits_enabled = False
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        result = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        assert isinstance(result, FinalOrderQuoteCheckResult)
        assert result.executable_price == Decimal("100.45")
        assert result.price_floor is None
        assert broker.submitted == []


def test_extended_hours_checks_apply_only_to_authorized_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    _session(monkeypatch)
    runner = _extended_runner(monkeypatch)
    with _execution_sandbox(runner, monkeypatch, _quote()) as (broker, _):
        runner._trade_svc.extended_hours_protective_exits_enabled = False
        legacy = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        assert isinstance(legacy, FinalOrderQuoteCheckResult)
        runner._trade_svc.extended_hours_protective_exits_enabled = True
        permission = runner._trade_svc.extended_hours_exit_decision(
            action="SELL", symbol="NVDA.US", market="US", reduce_only=True,
        )
        assert permission.permitted
        authorized = runner._validate_final_order_quote(runner.broker, "NVDA.US", "SELL", Decimal("100.45"))
        assert authorized == "extended-hours exit requires a registered price floor"
        assert broker.submitted == []


def test_rth_behavior_unchanged_for_price_stop(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given the 98.9/98.8/99 price-stop scenario from test_runner.py.
    _session(monkeypatch, "RTH")
    runner = _extended_runner(monkeypatch)
    runner.engine.params.buy_low = 95
    runner.engine.params.min_profit_amount = 1000
    def unexpected_policy(**kwargs: str | bool) -> None:
        pytest.fail("RTH must not consult the extended-hours permission policy")
    monkeypatch.setattr(runner._trade_svc, "extended_hours_exit_decision", unexpected_policy)
    broker = _FakeExtendedBroker()
    with _execution_sandbox(runner, monkeypatch, broker.quote):
        monkeypatch.setattr(runner, "broker", broker)
        # When the normal price stop runs through the quote callback.
        runner._on_quote(broker.quote)
        # Then the existing executable-bid price and four-argument call survive.
        assert broker.orders == [("NVDA.US", "SELL", Decimal("5"), Decimal("98.80"), None)]
