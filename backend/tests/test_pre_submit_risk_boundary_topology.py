from __future__ import annotations

import os
import inspect
import tempfile
from decimal import Decimal

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/"
    f"auto_trade_test_risk_topology_{os.getpid()}.db"
)

import pytest

from app.core.broker import BrokerGateway, OrderResult, Position, Quote
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services.trade_execution_service import (
    ApprovedOrder,
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)


class _TopologyBroker(BrokerGateway):
    def __init__(self, positions: list[Position] | None = None) -> None:
        self.positions = list(positions or [])
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []

    def get_positions(self) -> list[Position]:
        return list(self.positions)

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        return Decimal("10")

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        return OrderResult("topology-order", symbol, side, quantity, price, "SUBMITTED")


def _service() -> TradeExecutionService:
    return TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=lambda _reason: None,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        final_order_quote_check=(
            lambda _broker, _symbol, _action, price: FinalOrderQuoteCheckResult(
                executable_price=price,
                bid=price,
                ask=price,
            )
        ),
    )


def _install_boundary_spy(
    service: TradeExecutionService,
    monkeypatch: pytest.MonkeyPatch,
    invocations: list[str],
) -> None:
    boundary = getattr(service, "pre_submit_risk_check", None)
    assert callable(boundary), "mandatory pre-submit risk boundary is missing"

    def observe(*args, **kwargs):
        invocations.append("called")
        return boundary(*args, **kwargs)

    monkeypatch.setattr(service, "pre_submit_risk_check", observe)


def test_buy_submission_path_traverses_the_mandatory_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = _service()
    broker = _TopologyBroker()
    invocations: list[str] = []
    _install_boundary_spy(service, monkeypatch, invocations)

    # When
    status = service._execute_buy(
        "AAPL.US",
        Quote("AAPL.US", 100, 99.99, 100.01, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
    )

    # Then
    assert status is not None
    assert status.status == "SUBMITTED"
    assert invocations == ["called"]
    assert len(broker.submissions) == 1


def test_sell_short_submission_path_traverses_and_is_rejected_by_the_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    service = _service()
    broker = _TopologyBroker()
    invocations: list[str] = []
    _install_boundary_spy(service, monkeypatch, invocations)

    # When
    status = service._execute_sell_short(
        "AAPL.US",
        Quote("AAPL.US", 100, 99.99, 100.01, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
    )

    # Then
    assert status is not None
    assert status.status == "SKIPPED"
    assert invocations == ["called"]
    assert broker.submissions == []


@pytest.mark.parametrize(
    ("price", "allow_loss_exit"),
    [(Decimal("110"), False), (Decimal("90"), True)],
    ids=["reduce-only-profit-exit", "stop-loss-exit"],
)
def test_reduce_only_exit_traverses_boundary_without_entry_cap_enforcement(
    price: Decimal,
    allow_loss_exit: bool,
) -> None:
    # Given
    service = _service()
    service.max_position_quantity = None
    service.max_position_notional = None
    service.max_risk_per_trade = None
    service.stop_loss_pct = None
    broker = _TopologyBroker(
        [Position("AAPL.US", "LONG", Decimal("1"), Decimal("100"))],
    )
    probe_invocations: list[str] = []
    boundary = getattr(service, "pre_submit_risk_check", None)
    assert callable(boundary), "mandatory pre-submit risk boundary is missing"

    def observe(*args, **kwargs):
        probe_invocations.append("called")
        return boundary(*args, **kwargs)

    setattr(service, "pre_submit_risk_check", observe)

    # When
    status: OrderStatus | None = service.execute(
        "SELL",
        "AAPL.US",
        Quote(
            "AAPL.US",
            float(price),
            float(price - Decimal("0.01")),
            float(price),
            "",
        ),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
        allow_loss_exit=allow_loss_exit,
        reduce_only=True,
    )

    # Then
    assert status is not None
    assert status.status == "SUBMITTED"
    assert probe_invocations == ["called"]
    assert broker.submissions == [("AAPL.US", "SELL", Decimal("1"), price)]


def test_reduce_only_action_cannot_accept_an_independent_buy_side() -> None:
    # Given
    service = _service()
    broker = _TopologyBroker(
        [Position("AAPL.US", "LONG", Decimal("1"), Decimal("100"))],
    )
    parameters = inspect.signature(service._submit_limit_order).parameters

    # When / Then
    assert "side" not in parameters, "submission still accepts unchecked side"
    assert broker.submissions == [], "reduce-only SELL submitted an add-on BUY"


def test_entry_caps_apply_to_the_exact_submitted_price() -> None:
    # Given
    service = _service()
    service._final_order_quote_check = (
        lambda _broker, _symbol, _action, _price: FinalOrderQuoteCheckResult(
            executable_price=Decimal("100"),
            bid=Decimal("100"),
            ask=Decimal("100"),
        )
    )
    broker = _TopologyBroker()

    # When
    status = service._submit_limit_order(
        "BUY",
        "AAPL.US",
        Decimal("30"),
        Decimal("200"),
        broker,
        RiskController(),
        ServerChanNotifier(""),
    )

    # Then
    assert status is not None
    assert status.status == "SKIPPED"
    assert broker.submissions == [], "boundary approved $3,000 but submitted $6,000"


def test_boundary_rejects_add_on_even_when_compatibility_flag_is_forced() -> None:
    # Given
    service = _service()
    service.allow_position_addons = True
    broker = _TopologyBroker(
        [Position("AAPL.US", "LONG", Decimal("1"), Decimal("100"))],
    )

    # When
    status = service._submit_limit_order(
        "BUY",
        "AAPL.US",
        Decimal("1"),
        Decimal("100"),
        broker,
        RiskController(),
        ServerChanNotifier(""),
    )

    # Then
    assert status is not None
    assert status.status == "SKIPPED"
    assert broker.submissions == []


def test_forged_approved_order_has_no_broker_submission_seam() -> None:
    # Given
    service = _service()
    broker = _TopologyBroker()
    forged = ApprovedOrder(
        action="SELL",
        symbol="AAPL.US",
        side="BUY",
        quantity=Decimal("1000"),
        price=Decimal("1000"),
    )
    submit_after_precheck = getattr(
        service,
        "_submit_limit_order_after_precheck",
        None,
    )
    direct_approved_submit = getattr(service, "_submit_approved_order", None)

    # When
    if callable(submit_after_precheck):
        submit_after_precheck(
            forged,
            broker,
            RiskController(),
            ServerChanNotifier(""),
        )

    # Then
    assert submit_after_precheck is None
    assert direct_approved_submit is None
    assert broker.submissions == []
