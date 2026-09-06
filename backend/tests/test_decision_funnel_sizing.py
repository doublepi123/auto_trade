from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import runner as runner_module
from app.core.broker import BrokerGateway, OrderResult, Position, Quote
from app.core.engine import StrategyParams, TriggerResult
from app.runner import AppRunner, _QuoteTriggerDecision
from app.models import TradeEvent
from app.services import trade_execution_service as execution_module
from app.services.trade_execution_service import FinalOrderQuoteCheckResult


@pytest.fixture(autouse=True)
def _isolated_events(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    engine = create_engine("sqlite://")
    TradeEvent.metadata.create_all(engine, tables=[TradeEvent.metadata.tables["trade_events"]])
    monkeypatch.setattr(runner_module, "SessionLocal", sessionmaker(bind=engine))
    try:
        yield
    finally:
        engine.dispose()


class _FakeBroker(BrokerGateway):
    def __init__(self) -> None:
        self.quantity = Decimal("10")
        self.status = "SUBMITTED"
        self.submissions: list[OrderResult] = []
        self.sizing_calls = 0
        self.positions: list[Position] = []

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def estimate_margin_max_quantity(
        self, symbol: str, side: str, price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        self.sizing_calls += 1
        return self.quantity

    def submit_limit_order(
        self, symbol: str, side: str, quantity: Decimal, price: Decimal,
    ) -> OrderResult:
        result = OrderResult("funnel-sizing", symbol, side, quantity, price, self.status)
        self.submissions.append(result)
        return result


def _scenario(monkeypatch: pytest.MonkeyPatch) -> tuple[AppRunner, _FakeBroker]:
    runner = AppRunner()
    broker = _FakeBroker()
    runner.broker = broker
    runner.engine.params = StrategyParams(symbol="NVDA.US", market="US", buy_low=100, sell_high=110)
    monkeypatch.setattr(execution_module, "is_trading_hours", lambda _market: True)
    monkeypatch.setattr(execution_module, "is_opening_warmup", lambda *_args: False)
    monkeypatch.setattr(execution_module, "is_closing_window", lambda *_args: False)
    monkeypatch.setattr(runner, "_check_reconciliation_gate", lambda: True)
    monkeypatch.setattr(runner, "_broadcast_status", lambda: None)
    monkeypatch.setattr(runner, "_execution_ledger_context", lambda *_args: {})
    monkeypatch.setattr(runner, "_opening_execution_ledger_context", lambda _symbol, context: context)
    service = runner._trade_svc
    monkeypatch.setattr(service, "_record_order", lambda *_args: None)
    monkeypatch.setattr(service, "_record_risk_event", lambda *_args: None)
    monkeypatch.setattr(service, "_entry_policy_check", None)
    monkeypatch.setattr(service, "_final_order_quote_check", lambda _broker, _symbol, _action, price: FinalOrderQuoteCheckResult(executable_price=price, bid=price, ask=price))
    return runner, broker


def _drive(runner: AppRunner, symbol: str = "NVDA.US") -> None:
    runner._execute_triggered_order(
        _QuoteTriggerDecision(
            result=TriggerResult(True, "BUY", "sizing diagnostic"),
            trigger_engine=runner.engine, trigger_symbol=symbol, trigger_market="US",
        ),
        Quote(symbol, 100, 99.99, 100.01, datetime.now(timezone.utc).isoformat()),
    )


def test_positive_sizing_counts_when_boundary_vetoes(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: sizing succeeds, but the fresh executable price breaches the cap.
    runner, broker = _scenario(monkeypatch)
    monkeypatch.setattr(runner._trade_svc, "_final_order_quote_check", lambda *_args: FinalOrderQuoteCheckResult(executable_price=Decimal("10000"), bid=Decimal("10000"), ask=Decimal("10000")))
    # When
    _drive(runner)
    # Then
    snapshot = runner.decision_funnel.snapshot()
    assert broker.sizing_calls == 1
    assert snapshot.pre_submit_risk_check_invocations == 1
    assert snapshot.skips_by_category["RISK"] == 1
    assert broker.submissions == []
    assert snapshot.sized_quantity_positive == 1


def test_zero_sizing_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    runner, broker = _scenario(monkeypatch)
    broker.quantity = Decimal("0")
    # When
    _drive(runner)
    # Then
    snapshot = runner.decision_funnel.snapshot()
    assert snapshot.sized_quantity_positive == 0
    assert snapshot.skips_by_category["POSITION"] == 1
    assert broker.submissions == []


@pytest.mark.parametrize(("status", "acks"), [("SUBMITTED", 1), ("REJECTED", 0), ("CANCELLED", 0)])
def test_submission_counts_exactly_once(monkeypatch: pytest.MonkeyPatch, status: str, acks: int) -> None:
    # Given
    runner, broker = _scenario(monkeypatch)
    broker.status = status
    # When
    _drive(runner)
    # Then
    snapshot = runner.decision_funnel.snapshot()
    assert len(broker.submissions) == 1
    assert (snapshot.sized_quantity_positive, snapshot.submit_attempts, snapshot.broker_acks) == (1, 1, acks)


def test_nonprimary_sizing_does_not_count(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    runner, broker = _scenario(monkeypatch)
    # When
    _drive(runner, "AAPL.US")
    # Then
    assert broker.sizing_calls == 1
    assert len(broker.submissions) == 1
    assert runner.decision_funnel.snapshot().sized_quantity_positive == 0


def test_fee_skip_after_sizing_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given
    runner, broker = _scenario(monkeypatch)
    runner.engine.params.sell_high = 100.01
    # When
    _drive(runner)
    # Then
    snapshot = runner.decision_funnel.snapshot()
    assert snapshot.skips_by_category["FEE"] == 1
    assert broker.sizing_calls == 1
    assert broker.submissions == []
    assert snapshot.sized_quantity_positive == 1


@pytest.mark.parametrize(("action", "side"), [("SELL", "LONG"), ("BUY_TO_COVER", "SHORT")])
def test_exit_fee_skip_counts_positive_position_sizing(monkeypatch: pytest.MonkeyPatch, action: str, side: str) -> None:
    # Given
    runner, broker = _scenario(monkeypatch)
    broker.positions = [Position("NVDA.US", side, Decimal("1"), Decimal("100"))]
    # When
    status = runner._trade_svc.execute(action, "NVDA.US", Quote("NVDA.US", 100, 99.99, 100.01, ""), broker, runner.risk, runner.notifier, "USD", is_funnel_primary=True)
    # Then
    assert status is not None and status.status == "SKIPPED"
    snapshot = runner.decision_funnel.snapshot()
    assert snapshot.skips_by_category["FEE"] == 1
    assert snapshot.sized_quantity_positive == 1
    assert broker.submissions == []


def test_direct_short_sizing_counts_before_mandatory_veto(monkeypatch: pytest.MonkeyPatch) -> None:
    # Given: the otherwise unreachable short branch still has truthful accounting.
    runner, broker = _scenario(monkeypatch)
    # When
    status = runner._trade_svc._execute_sell_short("NVDA.US", Quote("NVDA.US", 100, 99.99, 100.01, ""), broker, runner.risk, runner.notifier, "USD", is_funnel_primary=True)
    # Then
    assert status is not None and status.status == "SKIPPED"
    snapshot = runner.decision_funnel.snapshot()
    assert snapshot.pre_submit_risk_check_invocations == 1
    assert snapshot.skips_by_category["RISK"] == 1
    assert snapshot.sized_quantity_positive == 1
    assert broker.submissions == []
