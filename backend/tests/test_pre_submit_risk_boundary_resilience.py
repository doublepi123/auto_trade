from __future__ import annotations

import os
import tempfile
import threading
from collections.abc import Callable
from decimal import Decimal

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/"
    f"auto_trade_test_risk_resilience_{os.getpid()}.db"
)

import pytest

from app.core.broker import BrokerGateway, OrderResult, Position, Quote
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services import trade_execution_service as trade_svc_module
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)


class _DatabaseWriteFailure(RuntimeError):
    pass


class _StalePositionFailure(RuntimeError):
    pass


class _FunnelProbe:
    def __init__(self) -> None:
        self.invocations = 0

    def record_pre_submit_risk_check(self) -> None:
        self.invocations += 1


class _FaultBroker(BrokerGateway):
    def __init__(
        self,
        *,
        margin_quantity: Decimal = Decimal("1111"),
        position_failure: RuntimeError | None = None,
        submit_timeout: bool = False,
        submit_release: threading.Event | None = None,
        submit_started: threading.Event | None = None,
    ) -> None:
        self.margin_quantity = margin_quantity
        self.position_failure = position_failure
        self.submit_timeout = submit_timeout
        self.submit_release = submit_release
        self.submit_started = submit_started
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []

    def get_positions(self) -> list[Position]:
        if self.position_failure is not None:
            raise self.position_failure
        return []

    def estimate_margin_max_quantity(
        self,
        symbol: str,
        side: str,
        price: Decimal,
        currency: str | None = None,
    ) -> Decimal:
        return self.margin_quantity

    def submit_limit_order(
        self,
        symbol: str,
        side: str,
        quantity: Decimal,
        price: Decimal,
    ) -> OrderResult:
        self.submissions.append((symbol, side, quantity, price))
        if self.submit_started is not None:
            self.submit_started.set()
        if self.submit_release is not None and not self.submit_release.wait(2):
            raise TimeoutError("test broker submission was not released")
        if self.submit_timeout:
            raise TimeoutError("broker submit timed out")
        return OrderResult("shared-order", symbol, side, quantity, price, "SUBMITTED")


@pytest.fixture(autouse=True)
def _market_is_open(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(trade_svc_module, "is_trading_hours", lambda _market: True)


def _ignore_risk_event(_reason: str) -> None:
    return None


def _service(
    record_risk_event: Callable[[str], None] = _ignore_risk_event,
    on_fill: Callable[[str, str], None] | None = None,
) -> TradeExecutionService:
    return TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=record_risk_event,
        on_fill=on_fill,
        margin_safety_factor=0.35,
        max_position_quantity=100,
        max_position_notional=5000.0,
        max_risk_per_trade=250.0,
        stop_loss_pct=1.0,
        full_buying_power_usage_enabled=True,
        final_order_quote_check=(
            lambda _broker, _symbol, _action, price: FinalOrderQuoteCheckResult(
                executable_price=price,
            )
        ),
    )


def _submit_oversized(
    service: TradeExecutionService,
    broker: BrokerGateway,
) -> OrderStatus | None:
    return service._submit_limit_order(
        "BUY",
        "PLTR.US",
        Decimal("1111"),
        Decimal("164.77"),
        broker,
        RiskController(),
        ServerChanNotifier(""),
    )


def _assert_submissions_within_caps(
    submissions: list[tuple[str, str, Decimal, Decimal]],
) -> None:
    assert all(quantity <= 100 for _, _, quantity, _ in submissions)
    assert all(quantity * price <= 5000 for _, _, quantity, price in submissions)


def test_risk_event_database_failure_cannot_open_the_broker_boundary() -> None:
    # Given
    def fail_risk_event_write(_reason: str) -> None:
        raise _DatabaseWriteFailure("risk event database unavailable")

    service = _service(record_risk_event=fail_risk_event_write)
    broker = _FaultBroker()
    observed_failure: _DatabaseWriteFailure | None = None
    status: OrderStatus | None = None

    # When
    try:
        status = _submit_oversized(service, broker)
    except _DatabaseWriteFailure as exc:
        observed_failure = exc

    # Then
    assert observed_failure is None, "risk-event persistence escaped the boundary"
    assert status is not None
    assert status.status == "SKIPPED"
    assert broker.submissions == [], "oversized entry reached broker after DB failure"


def test_quote_timeout_returns_skipped_without_broker_submission() -> None:
    # Given
    service = _service()

    def quote_timeout(*_args: object) -> FinalOrderQuoteCheckResult:
        raise TimeoutError("fresh quote timed out")

    service._final_order_quote_check = quote_timeout
    broker = _FaultBroker()
    observed_failure: TimeoutError | None = None
    status: OrderStatus | None = None

    # When
    try:
        status = _submit_oversized(service, broker)
    except TimeoutError as exc:
        observed_failure = exc

    # Then
    assert observed_failure is None, "quote timeout escaped the boundary"
    assert status is not None
    assert status.status == "SKIPPED"
    assert broker.submissions == []


def test_stale_position_data_rejects_inside_the_boundary() -> None:
    # Given
    service = _service()
    probe = _FunnelProbe()
    setattr(service, "decision_funnel", probe)
    broker = _FaultBroker(
        position_failure=_StalePositionFailure("position snapshot is stale"),
    )

    # When
    status = _submit_oversized(service, broker)

    # Then
    assert status is not None
    assert status.status == "SKIPPED"
    assert broker.submissions == []
    assert probe.invocations == 1, "stale position path bypassed the risk boundary"


def test_broker_timeout_never_receives_an_oversized_order() -> None:
    # Given
    service = _service()
    broker = _FaultBroker(submit_timeout=True)

    # When
    try:
        _submit_oversized(service, broker)
    except TimeoutError:
        pass

    # Then
    assert broker.submissions == [], "oversized order reached timing-out broker"


def test_duplicate_fill_callback_keeps_the_submitted_entry_within_caps() -> None:
    # Given
    fills: list[tuple[str, str]] = []
    service = _service(on_fill=lambda symbol, action: fills.append((symbol, action)))
    broker = _FaultBroker()

    # When
    status = service.execute(
        "BUY",
        "PLTR.US",
        Quote("PLTR.US", 164.77, 164.76, 164.78, ""),
        broker,
        RiskController(),
        ServerChanNotifier(""),
        "USD",
    )
    assert status is not None
    pending = service.pending_order_for("PLTR.US")
    assert pending is not None
    terminal = OrderStatus(
        pending.broker_order_id,
        "FILLED",
        executed_quantity=pending.quantity,
        executed_price=pending.price,
    )
    service._finalize_pending_fill(pending, terminal)
    service._finalize_pending_fill(pending, terminal)

    # Then
    _assert_submissions_within_caps(broker.submissions)
    assert fills == [("PLTR.US", "BUY")]


def test_two_concurrent_triggers_cannot_submit_an_oversized_entry() -> None:
    # Given
    release_submit = threading.Event()
    submit_started = threading.Event()
    broker = _FaultBroker(
        submit_release=release_submit,
        submit_started=submit_started,
    )
    service = _service()
    statuses: list[OrderStatus | None] = []
    errors: list[BaseException] = []

    def execute_buy() -> None:
        try:
            statuses.append(
                service.execute(
                    "BUY",
                    "PLTR.US",
                    Quote("PLTR.US", 164.77, 164.76, 164.78, ""),
                    broker,
                    RiskController(),
                    ServerChanNotifier(""),
                    "USD",
                )
            )
        except BaseException as exc:
            errors.append(exc)

    first = threading.Thread(target=execute_buy, daemon=True)
    second = threading.Thread(target=execute_buy, daemon=True)

    # When
    first.start()
    assert submit_started.wait(2)
    second.start()
    release_submit.set()
    first.join(2)
    second.join(2)

    # Then
    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert len(broker.submissions) == 1
    _assert_submissions_within_caps(broker.submissions)
    assert sorted(status.status for status in statuses if status is not None) == [
        "SKIPPED",
        "SUBMITTED",
    ]
