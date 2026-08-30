from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, DecimalException
from typing import Final

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/"
    f"auto_trade_test_pre_submit_risk_{os.getpid()}.db"
)

import pytest

from app.core.broker import BrokerGateway, OrderResult, Position
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskController
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderStatus,
    TradeExecutionService,
)
from tests.fixtures.pltr_risk_cap_bypass import PLTR_RISK_CAP_BYPASS_REPLAY


@dataclass(frozen=True, slots=True)
class _IncidentReplay:
    order_id: int
    symbol: str
    quantity: Decimal
    price: Decimal
    expected_status: str


_INCIDENT_REPLAYS: Final = (
    _IncidentReplay(
        order_id=52,
        symbol="NVDA.US",
        quantity=Decimal("1240"),
        price=Decimal("209.63"),
        expected_status="REJECTED",
    ),
    _IncidentReplay(
        order_id=54,
        symbol="ISRG.US",
        quantity=Decimal("704"),
        price=Decimal("371.49"),
        expected_status="REJECTED",
    ),
    _IncidentReplay(
        order_id=PLTR_RISK_CAP_BYPASS_REPLAY.order_id,
        symbol=PLTR_RISK_CAP_BYPASS_REPLAY.attempted_entry.symbol,
        quantity=Decimal(PLTR_RISK_CAP_BYPASS_REPLAY.attempted_entry.quantity),
        price=PLTR_RISK_CAP_BYPASS_REPLAY.attempted_entry.price,
        expected_status=PLTR_RISK_CAP_BYPASS_REPLAY.expected_outcome.status,
    ),
)


class _RecordingBroker(BrokerGateway):
    def __init__(self, margin_quantity: Decimal = Decimal("2000")) -> None:
        self.margin_quantity = margin_quantity
        self.submissions: list[tuple[str, str, Decimal, Decimal]] = []

    def get_positions(self) -> list[Position]:
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
        return OrderResult(
            f"submitted-{len(self.submissions)}",
            symbol,
            side,
            quantity,
            price,
            "SUBMITTED",
        )


class _FunnelProbe:
    def __init__(self) -> None:
        self.invocations = 0

    def record_pre_submit_risk_check(self) -> None:
        self.invocations += 1


def _service(
    risk_events: list[str],
    skipped_categories: list[str],
) -> TradeExecutionService:
    def record_skipped(
        _symbol: str,
        _action: str,
        _reason: str,
        payload: dict[str, object],
    ) -> None:
        skipped_categories.append(str(payload["skip_category"]))

    return TradeExecutionService(
        record_order=lambda *_args: None,
        update_order_status=lambda *_args: None,
        record_risk_event=risk_events.append,
        record_order_skipped=record_skipped,
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


@pytest.mark.parametrize(
    "incident",
    _INCIDENT_REPLAYS,
    ids=lambda incident: f"order-{incident.order_id}-{incident.symbol}",
)
def test_real_oversized_entry_is_rejected_when_full_power_flag_is_forced_true(
    incident: _IncidentReplay,
) -> None:
    # Given
    risk_events: list[str] = []
    skipped_categories: list[str] = []
    service = _service(risk_events, skipped_categories)
    broker = _RecordingBroker(margin_quantity=incident.quantity)

    # When
    status = service._submit_limit_order(
        "BUY",
        incident.symbol,
        incident.quantity,
        incident.price,
        broker,
        RiskController(),
        ServerChanNotifier(""),
    )

    # Then
    assert status is not None
    assert status.status == "SKIPPED", (
        f"paper order {incident.order_id} expected {incident.expected_status}; "
        f"observed broker submission status {status.status}"
    )
    assert broker.submissions == [], "oversized entry reached the fake broker"
    assert skipped_categories == ["RISK"]
    assert len(risk_events) == 1


@pytest.mark.parametrize(
    ("attribute", "invalid_value"),
    [
        ("max_position_quantity", None),
        ("max_position_quantity", float("nan")),
        ("max_position_notional", None),
        ("max_position_notional", float("inf")),
        ("max_risk_per_trade", None),
        ("max_risk_per_trade", float("nan")),
        ("stop_loss_pct", None),
        ("stop_loss_pct", float("inf")),
    ],
)
def test_missing_or_non_finite_entry_limit_fails_closed(
    attribute: str,
    invalid_value: int | float | None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given
    risk_events: list[str] = []
    service = _service(risk_events, [])
    monkeypatch.setattr(service, attribute, invalid_value)
    broker = _RecordingBroker()

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
    assert status.status == "SKIPPED", f"invalid {attribute} did not reject entry"
    assert broker.submissions == []
    assert len(risk_events) == 1


@pytest.mark.parametrize(
    "price",
    [Decimal("0"), Decimal("NaN"), Decimal("Infinity")],
)
def test_non_positive_or_non_finite_pre_submit_price_fails_closed(
    price: Decimal,
) -> None:
    # Given
    risk_events: list[str] = []
    service = _service(risk_events, [])
    broker = _RecordingBroker()
    status: OrderStatus | None = None

    # When
    try:
        status = service._submit_limit_order(
            "BUY",
            "AAPL.US",
            Decimal("1"),
            price,
            broker,
            RiskController(),
            ServerChanNotifier(""),
        )
    except DecimalException:
        pass

    # Then
    assert broker.submissions == [], f"invalid price {price} reached the fake broker"
    assert status is not None
    assert status.status == "SKIPPED", f"invalid price {price} did not reject entry"
    assert len(risk_events) == 1


def test_stale_final_price_is_rejected_inside_the_instrumented_boundary() -> None:
    # Given
    risk_events: list[str] = []
    service = _service(risk_events, [])
    service._final_order_quote_check = (
        lambda _broker, _symbol, _action, _price: "fresh quote is stale"
    )
    probe = _FunnelProbe()
    setattr(service, "decision_funnel", probe)
    broker = _RecordingBroker()

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
    assert probe.invocations == 1, "stale quote path bypassed the risk boundary"
    assert len(risk_events) == 1


class _LongPositionBroker(_RecordingBroker):
    def __init__(self, symbol: str, quantity: Decimal) -> None:
        super().__init__()
        self._position_symbol = symbol
        self._position_quantity = quantity

    def get_positions(self) -> list[Position]:
        return [
            Position(
                symbol=self._position_symbol,
                side="LONG",
                quantity=self._position_quantity,
                avg_price=Decimal("100"),
                available_quantity=self._position_quantity,
            )
        ]


def _submit_entry(
    service: TradeExecutionService,
    broker: BrokerGateway,
    risk: RiskController,
) -> OrderStatus | None:
    return service._submit_limit_order(
        "BUY",
        "AAPL.US",
        Decimal("1"),
        Decimal("100"),
        broker,
        risk,
        ServerChanNotifier(""),
    )


def _submit_reduce_only_exit(
    service: TradeExecutionService,
    broker: BrokerGateway,
    risk: RiskController,
) -> OrderStatus | None:
    return service._submit_limit_order(
        "SELL",
        "AAPL.US",
        Decimal("1"),
        Decimal("100"),
        broker,
        risk,
        ServerChanNotifier(""),
        reduce_only=True,
    )


def _submit_stop_loss_exit(
    service: TradeExecutionService,
    broker: BrokerGateway,
    risk: RiskController,
) -> OrderStatus | None:
    return service._submit_limit_order(
        "SELL",
        "AAPL.US",
        Decimal("1"),
        Decimal("100"),
        broker,
        risk,
        ServerChanNotifier(""),
        exit_allow_loss_exit=True,
    )


class TestTradingStateBoundaryMatrix:
    """3 states x 3 order kinds through the mandatory pre-submit boundary.

    The boundary consults the derived ``TradingState``; the outcome cells are
    the permission contract (ACTIVE = normal, REDUCING = exits only,
    HALTED = nothing).
    """

    # ----- ACTIVE -----
    def test_active_state_accepts_entry(self) -> None:
        # Given
        service = _service([], [])
        broker = _RecordingBroker()
        risk = RiskController()
        assert risk.trading_state().value == "ACTIVE"

        # When
        status = _submit_entry(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    def test_active_state_accepts_reduce_only_exit(self) -> None:
        # Given
        service = _service([], [])
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        assert risk.trading_state().value == "ACTIVE"

        # When
        status = _submit_reduce_only_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    def test_active_state_accepts_stop_loss_exit(self) -> None:
        # Given
        service = _service([], [])
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        assert risk.trading_state().value == "ACTIVE"

        # When
        status = _submit_stop_loss_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    # ----- REDUCING -----
    def test_reducing_state_rejects_entry(self) -> None:
        # Given
        skipped_categories: list[str] = []
        service = _service([], skipped_categories)
        broker = _RecordingBroker()
        risk = RiskController()
        risk.pause("manual stand-down")
        assert risk.trading_state().value == "REDUCING"

        # When
        status = _submit_entry(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []
        assert skipped_categories == ["RISK"]

    def test_reducing_state_accepts_reduce_only_exit(self) -> None:
        # Given
        service = _service([], [])
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        risk.pause("manual stand-down")
        assert risk.trading_state().value == "REDUCING"

        # When
        status = _submit_reduce_only_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    def test_reducing_state_accepts_stop_loss_exit(self) -> None:
        # Given
        service = _service([], [])
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        risk.pause("manual stand-down")
        assert risk.trading_state().value == "REDUCING"

        # When
        status = _submit_stop_loss_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SUBMITTED"
        assert len(broker.submissions) == 1

    # ----- HALTED -----
    def test_halted_state_rejects_entry(self) -> None:
        # Given
        skipped_categories: list[str] = []
        service = _service([], skipped_categories)
        broker = _RecordingBroker()
        risk = RiskController()
        risk.enable_kill_switch("test halt")
        assert risk.trading_state().value == "HALTED"

        # When
        status = _submit_entry(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []
        assert skipped_categories == ["RISK"]

    def test_halted_state_rejects_reduce_only_exit(self) -> None:
        # Given
        skipped_categories: list[str] = []
        service = _service([], skipped_categories)
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        risk.enable_kill_switch("test halt")
        assert risk.trading_state().value == "HALTED"

        # When
        status = _submit_reduce_only_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []
        assert skipped_categories == ["RISK"]

    def test_halted_state_rejects_stop_loss_exit(self) -> None:
        # Given
        skipped_categories: list[str] = []
        service = _service([], skipped_categories)
        broker = _LongPositionBroker("AAPL.US", Decimal("1"))
        risk = RiskController()
        risk.enable_kill_switch("test halt")
        assert risk.trading_state().value == "HALTED"

        # When
        status = _submit_stop_loss_exit(service, broker, risk)

        # Then
        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submissions == []
        assert skipped_categories == ["RISK"]
