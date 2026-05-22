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

    def test_execute_buy_uses_margin_max_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-1", "NVDA.US", "BUY", Decimal("90"), Decimal("222.5"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-1", "FILLED", Decimal("90"), Decimal("222.5")))

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.5, 222.4, 222.6, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_called_once_with("NVDA.US", "BUY", Decimal("222.5"), "USD")
        broker.get_cash.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("90"), Decimal("222.5"))

    def test_execute_buy_normalizes_us_limit_price_to_cent_tick(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-1", "NVDA.US", "BUY", Decimal("90"), Decimal("222.50"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-1", "FILLED", Decimal("90"), Decimal("222.50")))

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.509, 222.5, 222.51, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_called_once_with("NVDA.US", "BUY", Decimal("222.50"), "USD")
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("90"), Decimal("222.50"))

    def test_execute_buy_tracks_submitted_order_without_immediate_status_poll(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-live", "NVDA.US", "BUY", Decimal("90"), Decimal("222.5"), "SUBMITTED")
        broker.get_order_status.return_value = MagicMock(status="SUBMITTED")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.5, 222.4, 222.6, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SUBMITTED"
        assert svc.has_pending_order is True
        broker.get_order_status.assert_not_called()

    def test_execute_sell_short_uses_margin_max_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("50")
        broker.submit_limit_order.return_value = OrderResult("order-2", "NVDA.US", "SELL", Decimal("45"), Decimal("225"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-2", "FILLED", Decimal("45"), Decimal("225")))

        status = svc.execute(
            "SELL_SHORT",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_called_once_with("NVDA.US", "SELL", Decimal("225"), "USD")
        broker.get_cash.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("45"), Decimal("225"))

    def test_execute_buy_skips_zero_margin_quantity(self, svc: TradeExecutionService) -> None:
        from app.core.broker import Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("1")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 222.5, 222.4, 222.6, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is None
        broker.submit_limit_order.assert_not_called()

    def test_execute_sell_still_uses_position_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("7"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-3", "NVDA.US", "SELL", Decimal("7"), Decimal("225"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-3", "FILLED", Decimal("7"), Decimal("225")))

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("7"), Decimal("225"))

    def test_execute_sell_caps_quantity_to_available_long_position(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"), available_quantity=Decimal("4"))]
        broker.submit_limit_order.return_value = OrderResult("order-sell", "NVDA.US", "SELL", Decimal("4"), Decimal("225"), "FILLED")

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("4"), Decimal("225"))

    def test_execute_sell_normalizes_us_limit_price_to_cent_tick(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("7"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-3", "NVDA.US", "SELL", Decimal("7"), Decimal("225.51"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("order-3", "FILLED", Decimal("7"), Decimal("225.51")))

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 225.501, 225.49, 225.51, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("7"), Decimal("225.51"))

    def test_execute_sell_submits_even_when_price_does_not_cover_min_profit_buffer(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("7"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-sell", "NVDA.US", "SELL", Decimal("7"), Decimal("220.3"), "FILLED")

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 220.3, 220.29, 220.31, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("7"), Decimal("220.3"))

    def test_execute_sell_submits_even_when_expected_profit_below_min_amount(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-sell", "NVDA.US", "SELL", Decimal("10"), Decimal("220.4"), "FILLED")

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 220.4, 220.39, 220.41, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("5"),
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("10"), Decimal("220.4"))

    def test_execute_sell_allows_stop_loss_exit_below_min_profit(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("stop-loss-1", "NVDA.US", "SELL", Decimal("10"), Decimal("215"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("stop-loss-1", "FILLED", Decimal("10"), Decimal("215")))

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("50"),
            allow_loss_exit=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "SELL", Decimal("10"), Decimal("215"))

    def test_execute_sell_uses_weighted_tracked_entry_price_when_broker_avg_is_stale(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("200"), Decimal("98"))]
        broker.submit_limit_order.return_value = OrderResult("weighted-sell", "NVDA.US", "SELL", Decimal("200"), Decimal("105"), "FILLED")
        risk = RiskController()
        svc._record_entry_price("NVDA.US", Decimal("100"), Decimal("100"))
        svc._record_entry_price("NVDA.US", Decimal("104"), Decimal("100"))

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 105, 104.9, 105.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "FILLED"
        assert risk.daily_pnl == 600.0

    def test_execute_buy_to_cover_submits_even_when_expected_profit_below_min_amount(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "SHORT", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("order-cover", "NVDA.US", "BUY", Decimal("10"), Decimal("219.7"), "FILLED")

        status = svc.execute(
            "BUY_TO_COVER",
            "NVDA.US",
            Quote("NVDA.US", 219.7, 219.69, 219.71, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("5"),
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("10"), Decimal("219.7"))

    def test_execute_buy_to_cover_caps_quantity_to_available_short_position(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "SHORT", Decimal("12"), Decimal("220"), available_quantity=Decimal("5"))]
        broker.submit_limit_order.return_value = OrderResult("order-cover", "NVDA.US", "BUY", Decimal("5"), Decimal("219"), "FILLED")

        status = svc.execute(
            "BUY_TO_COVER",
            "NVDA.US",
            Quote("NVDA.US", 219, 218.9, 219.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("5"), Decimal("219"))

    def test_execute_buy_to_cover_allows_stop_loss_exit_below_min_profit(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "SHORT", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("stop-cover-1", "NVDA.US", "BUY", Decimal("10"), Decimal("225"), "FILLED")
        monkeypatch.setattr(svc, "_wait_for_order_completion", lambda result, broker_arg=None: OrderStatus("stop-cover-1", "FILLED", Decimal("10"), Decimal("225")))

        status = svc.execute(
            "BUY_TO_COVER",
            "NVDA.US",
            Quote("NVDA.US", 225, 224.9, 225.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("50"),
            allow_loss_exit=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with("NVDA.US", "BUY", Decimal("10"), Decimal("225"))

    def test_execute_buy_to_cover_uses_weighted_tracked_entry_price_when_broker_avg_is_stale(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "SHORT", Decimal("200"), Decimal("102"))]
        broker.submit_limit_order.return_value = OrderResult("weighted-cover", "NVDA.US", "BUY", Decimal("200"), Decimal("95"), "FILLED")
        risk = RiskController()
        svc._record_entry_price("NVDA.US", Decimal("100"), Decimal("100"))
        svc._record_entry_price("NVDA.US", Decimal("96"), Decimal("100"))

        status = svc.execute(
            "BUY_TO_COVER",
            "NVDA.US",
            Quote("NVDA.US", 95, 94.9, 95.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "FILLED"
        assert risk.daily_pnl == 600.0

    def test_cancel_pending_order_calls_broker_and_restores_snapshot(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderResult, OrderStatusResult
        from app.core.engine import EngineState

        updates = []
        svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status))
        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult("order-1", "CANCELLED")
        snapshot = (EngineState.LONG, 221.0, None)
        restored = []
        svc._track_pending_order(
            "BUY",
            OrderResult("order-1", "NVDA.US", "BUY", Decimal("10"), Decimal("221.5"), "SUBMITTED"),
            broker,
            snapshot,
        )

        result = svc.cancel_pending_order(restore_engine_snapshot=restored.append)

        assert result.status == "CANCELLED"
        assert svc.has_pending_order is False
        broker.cancel_order.assert_called_once_with("order-1")
        assert updates[-1] == ("order-1", "CANCELLED")
        assert restored == [snapshot]

    def test_cancel_pending_order_returns_no_pending_when_empty(self, svc: TradeExecutionService) -> None:
        result = svc.cancel_pending_order()

        assert result.status == "NO_PENDING_ORDER"
