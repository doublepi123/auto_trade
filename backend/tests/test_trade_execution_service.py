from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock

import pytest

from app.services.trade_execution_service import TradeExecutionService, OrderStatus


class TestOrderStatus:
    def test_dataclass_fields(self) -> None:
        s = OrderStatus(broker_order_id="123", status="FILLED", executed_quantity=Decimal("10"), executed_price=Decimal("150"))
        assert s.broker_order_id == "123"
        assert s.status == "FILLED"
        assert s.executed_quantity == Decimal("10")
        assert s.executed_price == Decimal("150")

    def test_defaults(self) -> None:
        s = OrderStatus(broker_order_id="456", status="SUBMITTED")
        assert s.executed_quantity == Decimal("0")
        assert s.executed_price == Decimal("0")


class TestTradeExecutionServiceBasics:
    @pytest.fixture
    def svc(self) -> TradeExecutionService:
        return TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )

    def test_has_pending_order_false_initially(self, svc: TradeExecutionService) -> None:
        assert svc.has_pending_order is False

    def test_resolved_decimal_positive(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "150.50"
        result = svc._resolved_decimal(item, "executed_price", Decimal("0"))
        assert result == Decimal("150.50")

    def test_resolved_decimal_zero_fallback(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "0"
        result = svc._resolved_decimal(item, "executed_price", Decimal("99"))
        assert result == Decimal("99")

    def test_resolved_decimal_invalid_fallback(self, svc: TradeExecutionService) -> None:
        item = MagicMock()
        item.executed_price = "invalid"
        result = svc._resolved_decimal(item, "executed_price", Decimal("99"))
        assert result == Decimal("99")

    def test_wait_for_order_completion_already_filled(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult
        result = OrderResult(
            broker_order_id="1",
            symbol="AAPL.US",
            side="BUY",
            quantity=Decimal("10"),
            price=Decimal("150"),
            status="FILLED",
        )
        status = svc._wait_for_order_completion(result, None)
        assert status.status == "FILLED"

    def test_wait_for_order_completion_rejected(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult
        result = OrderResult(
            broker_order_id="1",
            symbol="AAPL.US",
            side="BUY",
            quantity=Decimal("10"),
            price=Decimal("150"),
            status="SUBMITTED",
        )
        broker = MagicMock()
        broker.get_order_status.return_value = MagicMock(
            status="REJECTED",
            executed_quantity=Decimal("0"),
            executed_price=Decimal("0"),
            broker_order_id="1",
        )
        status = svc._wait_for_order_completion(result, broker)
        assert status.status == "REJECTED"

    def test_coerce_order_status(self, svc: TradeExecutionService) -> None:
        raw = MagicMock()
        raw.status = "FILLED"
        raw.broker_order_id = "abc"
        raw.executed_quantity = Decimal("5")
        raw.executed_price = Decimal("100")
        status = svc._coerce_order_status(raw, "default")
        assert status.status == "FILLED"
        assert status.broker_order_id == "abc"

    def test_order_status_is_live(self, svc: TradeExecutionService) -> None:
        assert svc._order_status_is_live(MagicMock(status="SUBMITTED")) is True
        assert svc._order_status_is_live(MagicMock(status="PARTIAL_FILLED")) is True
        assert svc._order_status_is_live(MagicMock(status="FILLED")) is False
        assert svc._order_status_is_live(MagicMock(status="REJECTED")) is False
