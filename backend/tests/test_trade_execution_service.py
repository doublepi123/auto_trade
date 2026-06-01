# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.broker import OrderResult
from app.services.trade_execution_service import TradeExecutionService, OrderStatus, _PendingOrder


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

    def test_reconcile_pending_order_times_out(self, svc: TradeExecutionService) -> None:
        from app.core.risk import RiskController

        broker = MagicMock()
        broker.get_order_status.return_value = SimpleNamespace(
            broker_order_id="order-1",
            status="SUBMITTED",
            executed_quantity=Decimal("0"),
            executed_price=Decimal("0"),
        )
        risk = RiskController()
        restored: list[object] = []
        pending = _PendingOrder(
            broker=broker,
            broker_order_id="order-1",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("10"),
            price=Decimal("100"),
            engine_snapshot=None,
            next_status_check_at=0.0,
            submitted_at=time.monotonic() - 60,
        )
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 0
        svc._reconcile_pending_order(
            pending,
            risk=risk,
            restore_engine_snapshot=lambda snapshot: restored.append(snapshot),
        )

        assert risk.paused is True
        assert svc.has_pending_order is False

    def test_persist_failure_pauses_and_attempts_cancel(self, svc: TradeExecutionService) -> None:
        from app.core.broker import BrokerGateway
        from app.core.risk import RiskController

        risk = RiskController()
        broker = MagicMock(spec=BrokerGateway)
        broker.cancel_order.return_value = SimpleNamespace(status="CANCELLED")
        svc._record_order = MagicMock(side_effect=RuntimeError("db down"))

        result = OrderResult("order-1", "AAPL.US", "BUY", Decimal("10"), Decimal("100"), "SUBMITTED")
        status = svc._recover_from_missing_order_record(result, broker, risk)

        assert status.status == "REJECTED"
        assert risk.paused is True
        broker.cancel_order.assert_called_once_with("order-1")

    def test_execute_buy_uses_margin_max_quantity(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-1", "NVDA.US", "BUY", Decimal("90"), Decimal("222.5"), "FILLED")

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

    def test_normalize_limit_price_hk_tier_boundaries(self) -> None:
        # 0.243 lives in the 0.005-tick tier (≥0.25 is not yet reached → 0.001 tier)
        # Actually 0.243 < 0.25 → 0.001 tick tier. BUY rounds down, SELL rounds up.
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("0.2433")) == Decimal("0.243")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("0.2431")) == Decimal("0.244")
        # 0.30 → 0.005 tick tier (>=0.25 and <0.50)
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("0.303")) == Decimal("0.300")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("0.301")) == Decimal("0.305")
        # 5.00 → 0.01 tick tier (>=0.5 and <10)
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("5.005")) == Decimal("5.00")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("5.005")) == Decimal("5.01")
        # Phase 1: 10-20 now trades in 0.01 ticks.
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("15.017")) == Decimal("15.01")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("15.011")) == Decimal("15.02")
        # Phase 1: 20-50 now trades in 0.02 ticks.
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("25.037")) == Decimal("25.02")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("25.037")) == Decimal("25.04")
        # 150.04 -> 0.10 tick tier (>=100 and <200): BUY floors to 150.00, SELL ceils to 150.10.
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("150.04")) == Decimal("150.00")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("150.01")) == Decimal("150.10")

    def test_normalize_limit_price_passes_through_non_us_non_hk(self) -> None:
        # Markets without explicit tick mapping must not silently mutate the price.
        price = Decimal("1234.5678")
        assert TradeExecutionService._normalize_limit_price("FOO.SG", "BUY", price) == price

    def test_load_tracked_entries_round_trip(self, svc: TradeExecutionService) -> None:
        svc.load_tracked_entries({
            "AAPL.US": (Decimal("100"), Decimal("15000")),
            "0700.HK": (Decimal("0"), Decimal("0")),  # ignored
        })
        snapshot = svc.snapshot_tracked_entries()
        assert "0700.HK" not in snapshot
        assert snapshot["AAPL.US"] == (Decimal("100"), Decimal("15000"))

    def test_persist_callback_invoked_on_entry_and_exit(self) -> None:
        calls: list[tuple[str, Decimal, Decimal]] = []

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            persist_entry=lambda symbol, qty, cost: calls.append((symbol, qty, cost)),
        )

        svc._record_entry_price("AAPL.US", Decimal("150"), Decimal("10"))
        assert calls[-1] == ("AAPL.US", Decimal("10"), Decimal("1500"))

        svc._record_entry_price("AAPL.US", Decimal("160"), Decimal("10"))
        assert calls[-1] == ("AAPL.US", Decimal("20"), Decimal("3100"))

        svc._consume_entry_quantity("AAPL.US", Decimal("5"))
        symbol, qty, _cost = calls[-1]
        assert symbol == "AAPL.US"
        assert qty == Decimal("15")

        svc._consume_entry_quantity("AAPL.US", Decimal("15"))
        assert calls[-1] == ("AAPL.US", Decimal("0"), Decimal("0"))

    def test_persist_callback_failure_does_not_break_flow(self, svc: TradeExecutionService) -> None:
        def failing_callback(symbol: str, qty: Decimal, cost: Decimal) -> None:
            raise RuntimeError("persist failed")

        svc._persist_entry = failing_callback
        # Should not raise.
        svc._record_entry_price("AAPL.US", Decimal("10"), Decimal("5"))
        assert svc.snapshot_tracked_entries()["AAPL.US"][0] == Decimal("5")

    def test_restart_recovery_uses_tracked_avg_for_exit_pnl(self, svc: TradeExecutionService) -> None:
        # Simulate a restart: tracked entries restored from DB, broker reports a stale avg_price.
        svc.load_tracked_entries({"AAPL.US": (Decimal("10"), Decimal("1000"))})  # avg = 100
        broker_avg_price = Decimal("90")  # stale
        avg = svc._resolve_avg_price_for_exit("AAPL.US", broker_avg_price, Decimal("10"))
        # Tracked avg (100) differs by >$2 from broker (90) -> use tracked.
        assert avg == Decimal("100")

    def test_execute_buy_normalizes_us_limit_price_to_cent_tick(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.submit_limit_order.return_value = OrderResult("order-1", "NVDA.US", "BUY", Decimal("90"), Decimal("222.50"), "FILLED")

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

    def test_execute_sell_skips_when_price_does_not_cover_min_profit_buffer(self, svc: TradeExecutionService, monkeypatch) -> None:
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
        assert status.status == "SKIPPED"
        assert "below required minimum profit" in status.reason
        broker.submit_limit_order.assert_not_called()

    def test_execute_sell_skips_when_expected_profit_below_min_amount(self, svc: TradeExecutionService, monkeypatch) -> None:
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
        assert status.status == "SKIPPED"
        assert "below required minimum profit" in status.reason
        broker.submit_limit_order.assert_not_called()

    def test_execute_sell_records_skipped_precheck_event(self, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]

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
        assert status.status == "SKIPPED"
        assert skipped
        assert skipped[0][0] == "NVDA.US"
        assert skipped[0][1] == "SELL"
        assert skipped[0][3]["expected_profit"] == 4.0
        assert skipped[0][3]["skip_category"] == "FEE"

    def test_execute_sell_allows_stop_loss_exit_below_min_profit(self, svc: TradeExecutionService, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("stop-loss-1", "NVDA.US", "SELL", Decimal("10"), Decimal("215"), "FILLED")

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

    def test_execute_buy_to_cover_skips_when_expected_profit_below_min_amount(self, svc: TradeExecutionService, monkeypatch) -> None:
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
        assert status.status == "SKIPPED"
        assert "below required minimum profit" in status.reason
        broker.submit_limit_order.assert_not_called()

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

    def test_execute_sell_skips_when_fees_reduce_net_profit_below_minimum(self, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("100"))]
        broker.submit_limit_order.return_value = OrderResult("order-fee", "NVDA.US", "SELL", Decimal("10"), Decimal("101"), "FILLED")

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("9"),
            fee_rate=Decimal("0.001"),
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped, "expected record_order_skipped to be called"
        payload = skipped[0][3]
        assert payload["skip_category"] == "FEE"
        assert abs(float(payload["estimated_fees"]) - 2.01) < 0.001
        broker.submit_limit_order.assert_not_called()

    def test_execute_sell_stop_loss_does_not_apply_fee_gate(self, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.2)
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"))]
        broker.submit_limit_order.return_value = OrderResult("stop-loss-fee", "NVDA.US", "SELL", Decimal("10"), Decimal("215"), "FILLED")
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )

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
            fee_rate=Decimal("0.001"),
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once()

    def test_execute_risk_rejection_records_risk_skip_category(self) -> None:
        from app.core.broker import Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        risk = RiskController()
        risk.pause("test risk rejection")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 220, 219.9, 220.1, ""),
            MagicMock(),
            risk,
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped, "expected record_order_skipped to be called for risk rejection"
        assert skipped[0][3]["skip_category"] == "RISK"

    def test_execute_pending_rejection_records_pending_skip_category(self) -> None:
        from app.core.broker import OrderResult, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult("order-live", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            broker,
            None,
        )

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 221, 220.9, 221.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped, "expected record_order_skipped to be called for pending guard"
        assert skipped[0][3]["skip_category"] == "PENDING"

    def test_execute_sell_without_available_quantity_records_position_category(self) -> None:
        from app.core.broker import Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("NVDA.US", "LONG", Decimal("10"), Decimal("220"), available_quantity=Decimal("0"))
        ]

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
        assert status.status == "SKIPPED"
        assert skipped, "expected record_order_skipped to be called for zero available quantity"
        assert skipped[0][3]["skip_category"] == "POSITION"
        broker.submit_limit_order.assert_not_called()

    def test_execute_buy_to_cover_skips_when_fees_reduce_net_profit_below_minimum(self, monkeypatch) -> None:
        from app.config import settings
        from app.core.broker import OrderResult, Position, Quote
        from app.core.risk import RiskController
        from app.core.notify import ServerChanNotifier

        monkeypatch.setattr(settings, "min_exit_profit_pct", 0.0)
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        broker = MagicMock()
        broker.get_positions.return_value = [Position("NVDA.US", "SHORT", Decimal("10"), Decimal("102"))]
        broker.submit_limit_order.return_value = OrderResult("order-btc-fee", "NVDA.US", "BUY_TO_COVER", Decimal("10"), Decimal("101"), "FILLED")

        status = svc.execute(
            "BUY_TO_COVER",
            "NVDA.US",
            Quote("NVDA.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            min_profit_amount=Decimal("9"),
            fee_rate=Decimal("0.001"),
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped, "expected record_order_skipped to be called"
        payload = skipped[0][3]
        assert payload["skip_category"] == "FEE"
        assert abs(float(payload["estimated_fees"]) - 2.03) < 0.001
        broker.submit_limit_order.assert_not_called()
