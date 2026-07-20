# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.core.broker import OrderResult, Quote
from app.core.notify import ServerChanNotifier
from app.core.risk import RiskConfig, RiskController
from app.services import trade_execution_service as trade_svc_module
from app.services.trade_execution_service import (
    FinalOrderQuoteCheckResult,
    OrderPersistenceError,
    OrderStatus,
    TradeExecutionService,
    _PendingOrder,
)


class TestOrderStatus:
    def test_dataclass_fields(self) -> None:
        s = OrderStatus(broker_order_id="123", status="FILLED", executed_quantity=Decimal("10"), executed_price=Decimal("150"))
        assert s.broker_order_id == "123"
        assert s.status == "FILLED"
        assert s.executed_quantity == Decimal("10")
        assert s.executed_price == Decimal("150")

    def test_defaults(self) -> None:
        s = OrderStatus(broker_order_id="456", status="SUBMITTED")
        # Default is None (no fill reported); downstream consumers must go
        # through OrderStatus._positive to compare or compute.
        assert s.executed_quantity is None
        assert s.executed_price is None
        assert OrderStatus._positive(s.executed_quantity) == Decimal("0")


class TestTradeExecutionServiceBasics:
    @pytest.fixture(autouse=True)
    def _market_is_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            trade_svc_module,
            "is_trading_hours",
            lambda _market: True,
        )

    @pytest.fixture
    def svc(self) -> TradeExecutionService:
        return TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            final_order_quote_check=lambda _broker, _symbol, _action, price: (
                FinalOrderQuoteCheckResult(executable_price=price)
            ),
        )

    def test_has_pending_order_false_initially(self, svc: TradeExecutionService) -> None:
        assert svc.has_pending_order is False

    def test_load_pending_orders_preserves_all_ids_for_same_symbol(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        pending_orders = [
            _PendingOrder(
                broker=broker,
                broker_order_id=order_id,
                symbol="AAPL.US",
                action="BUY",
                quantity=Decimal("1"),
                price=Decimal("100"),
                engine_snapshot=None,
            )
            for order_id in ("duplicate-1", "duplicate-2")
        ]

        svc.load_pending_orders(pending_orders)

        assert svc.pending_order_ids() == ["duplicate-1", "duplicate-2"]
        assert [
            pending.broker_order_id
            for pending in svc.pending_orders_for("AAPL.US")
        ] == ["duplicate-1", "duplicate-2"]
        assert svc.pending_order_by_broker_id("duplicate-1") is not None
        assert svc.pending_order_by_broker_id("duplicate-2") is not None

    def test_live_entry_safety_defaults_deny_addons_and_short_entries(
        self,
        svc: TradeExecutionService,
    ) -> None:
        assert svc.allow_position_addons is False
        assert svc.short_entries_enabled is False

    def test_any_session_mode_blocks_buy_outside_trading_hours(
        self,
        svc: TradeExecutionService,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(
            trade_svc_module,
            "is_trading_hours",
            lambda _market: False,
        )
        broker = MagicMock()

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 100, 99.9, 100.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            market="US",
            trading_session_mode="ANY",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "ANY mode cannot open a long position" in status.reason
        broker.get_positions.assert_not_called()
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_not_called()

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
        status = svc._coerce_order_status(raw, "abc")
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
        svc.load_pending_orders([pending])
        svc._reconcile_pending_order(
            pending,
            risk=risk,
            restore_engine_snapshot=lambda snapshot: restored.append(snapshot),
        )

        assert risk.paused is True
        assert risk.pause_auto_resumable is False
        assert svc.has_pending_order is True

    def test_reconcile_pending_order_query_failure_is_throttled_and_warned(
        self,
        svc: TradeExecutionService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("broker detail temporarily unavailable")
        pending = _PendingOrder(
            broker=broker,
            broker_order_id="order-query-fails",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("10"),
            price=Decimal("100"),
            engine_snapshot=None,
            next_status_check_at=0.0,
            submitted_at=time.monotonic(),
        )
        svc._order_status_timeout_seconds = 30
        svc._order_status_poll_interval_seconds = 10
        svc.load_pending_orders([pending])

        caplog.set_level(logging.WARNING, logger="auto_trade.services.trade_execution_service")
        svc.reconcile()
        svc.reconcile()

        assert broker.get_order_status.call_count == 1
        assert svc.pending_order_for("AAPL.US") is not None
        records = [
            rec
            for rec in caplog.records
            if "failed to query pending order status for order-query-fails" in rec.message
        ]
        assert len(records) == 1
        assert records[0].levelno == logging.WARNING
        assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)

    def test_pending_timeout_broker_failures_pause_without_error_logs(
        self,
        svc: TradeExecutionService,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from app.core.risk import RiskController

        broker = MagicMock()
        broker.get_order_status.side_effect = RuntimeError("broker order detail unavailable")
        broker.cancel_order.side_effect = RuntimeError("broker cancel unavailable")
        pending = _PendingOrder(
            broker=broker,
            broker_order_id="order-timeout-fails",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("10"),
            price=Decimal("100"),
            engine_snapshot=None,
            next_status_check_at=0.0,
            submitted_at=time.monotonic() - 60,
        )
        risk = RiskController()
        updates: list[tuple[str, str]] = []
        svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status))
        svc._order_status_timeout_seconds = 30
        svc.load_pending_orders([pending])

        caplog.set_level(logging.WARNING, logger="auto_trade.services.trade_execution_service")
        svc.reconcile(risk=risk)

        assert risk.paused is True
        assert svc.has_pending_order is True
        assert updates == []
        messages = [rec.message for rec in caplog.records]
        assert any("failed to query pending order status during timeout for order-timeout-fails" in msg for msg in messages)
        assert any("failed to cancel timed-out order order-timeout-fails" in msg for msg in messages)
        assert any("failed to recover partial fill after timeout for order-timeout-fails" in msg for msg in messages)
        assert not any(rec.levelno >= logging.ERROR for rec in caplog.records)

    def test_persist_failure_pauses_and_attempts_cancel(self, svc: TradeExecutionService) -> None:
        from app.core.broker import BrokerGateway
        from app.core.risk import RiskController

        risk = RiskController()
        broker = MagicMock(spec=BrokerGateway)
        broker.cancel_order.return_value = SimpleNamespace(
            broker_order_id="order-1",
            status="CANCELLED",
        )
        svc._record_order = MagicMock(side_effect=RuntimeError("db down"))

        result = OrderResult("order-1", "AAPL.US", "BUY", Decimal("10"), Decimal("100"), "SUBMITTED")
        status = svc._recover_from_missing_order_record(result, broker, risk)

        assert status.status == "CANCELLED"
        assert risk.paused is True
        broker.cancel_order.assert_called_once_with("order-1")

    def test_persist_failure_keeps_unconfirmed_orphan_pending(
        self,
        svc: TradeExecutionService,
    ) -> None:
        risk = RiskController()
        broker = MagicMock()
        broker.cancel_order.side_effect = TimeoutError("cancel outcome unknown")
        result = OrderResult(
            "order-unknown",
            "AAPL.US",
            "SELL",
            Decimal("10"),
            Decimal("100"),
            "SUBMITTED",
        )

        status = svc._recover_from_missing_order_record(
            result,
            broker,
            risk,
            action="SELL",
        )

        assert status.status == "SUBMITTED"
        assert svc.pending_order_for("AAPL.US") is not None
        assert risk.paused is True
        assert risk.pause_auto_resumable is False

    def test_recovered_filled_submit_finalizes_exit_only_once(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import OrderStatusResult, Position

        record_calls = 0
        order_persisted = False
        update_calls = 0
        update_args: list[tuple[object, ...]] = []
        filled_symbols: list[str] = []

        def record_order(*_args: object) -> None:
            nonlocal record_calls, order_persisted
            record_calls += 1
            if record_calls == 1:
                raise RuntimeError("initial order persistence failed")
            order_persisted = True

        def update_order_status(*args: object) -> None:
            nonlocal update_calls
            update_calls += 1
            update_args.append(args)
            if not order_persisted:
                raise RuntimeError("order record does not exist yet")

        class Broker:
            def get_positions(self) -> list[Position]:
                return [
                    Position(
                        "AAPL.US",
                        "LONG",
                        Decimal("5"),
                        Decimal("100"),
                        available_quantity=Decimal("2"),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                return OrderResult(
                    "recovered-filled-exit",
                    symbol,
                    side,
                    quantity,
                    price,
                    "SUBMITTED",
                )

            def cancel_order(self, order_id: str) -> OrderStatusResult:
                return OrderStatusResult(
                    order_id,
                    "FILLED",
                    executed_quantity=Decimal("2"),
                    executed_price=Decimal("110"),
                )

        svc._record_order = record_order
        svc._update_order_status = update_order_status
        svc._on_fill = lambda symbol, _action: filled_symbols.append(symbol)
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("5"))
        risk = RiskController()

        status = svc.execute(
            "SELL",
            "AAPL.US",
            Quote("AAPL.US", 110, 109.9, 110.1, ""),
            Broker(),
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        assert status.fill_finalized is True
        assert record_calls == 2
        assert update_calls == 3
        assert sum(
            1
            for args in update_args
            if len(args) == 6
            and isinstance(args[5], dict)
            and args[5].get("pnl_source") == "TRACKED_ENTRY"
        ) == 1
        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")
        assert tracked.cost == Decimal("300")
        assert risk.daily_pnl == 20.0
        assert filled_symbols == ["AAPL.US"]

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

    def test_margin_safety_factor_instance_attribute(self, svc: TradeExecutionService) -> None:
        assert svc.margin_safety_factor is None
        svc.margin_safety_factor = 0.75
        assert svc.margin_safety_factor == 0.75

    def test_entry_quantity_uses_instance_margin_safety_factor(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = 0.75
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD")
        assert qty == 75  # 100 * 0.75

    def test_entry_quantity_keyword_overrides_instance_attribute(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = 0.75
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD", safety_factor=0.5)
        assert qty == 50  # 100 * 0.5

    def test_entry_quantity_fallback_to_constant(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = None
        qty = svc._entry_quantity_from_margin_power(broker, "AAPL.US", "BUY", Decimal("150"), "USD")
        assert qty == 90  # 100 * 0.9 (ENTRY_BUYING_POWER_USAGE)

    def test_entry_quantity_respects_all_hard_caps(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("1000")
        svc.margin_safety_factor = 1.0
        svc.max_position_quantity = 100
        svc.max_position_notional = 1000
        svc.max_risk_per_trade = 50
        svc.stop_loss_pct = 1.0

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 10

    def test_existing_position_consumes_quantity_and_notional_headroom(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.margin_safety_factor = 1.0
        svc.allow_position_addons = True
        svc.max_position_quantity = 10
        svc.max_position_notional = 1000
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("8"))

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 2

    @pytest.mark.parametrize("position_source", ["broker", "tracked"])
    def test_existing_position_consumes_risk_headroom(
        self,
        svc: TradeExecutionService,
        position_source: str,
    ) -> None:
        from app.core.broker import Position

        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("1000")
        if position_source == "broker":
            broker.get_positions.return_value = [
                Position("AAPL.US", "LONG", Decimal("80"), Decimal("100"))
            ]
        else:
            broker.get_positions.return_value = []
            svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("80"))
        svc.margin_safety_factor = 1.0
        svc.allow_position_addons = True
        svc.max_risk_per_trade = 100
        svc.stop_loss_pct = 1.0

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 20

    def test_entry_caps_fail_closed_when_broker_position_lookup_fails(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        broker.get_positions.side_effect = RuntimeError("position API unavailable")
        svc.max_position_quantity = 10

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 0

    def test_entry_caps_fail_closed_when_broker_position_reader_is_missing(
        self,
        svc: TradeExecutionService,
    ) -> None:
        class BrokerWithoutPositions:
            estimate_calls = 0

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                self.estimate_calls += 1
                return Decimal("100")

        broker = BrokerWithoutPositions()
        svc.allow_position_addons = True
        svc.max_position_quantity = 10

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 0
        assert broker.estimate_calls == 0

    def test_final_sizing_rechecks_addon_policy_against_broker_truth(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import Position

        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("AAPL.US", "LONG", Decimal("1"), Decimal("100"))
        ]
        broker.estimate_margin_max_quantity.return_value = Decimal("100")
        svc.allow_position_addons = False
        svc.max_position_quantity = 10

        qty = svc._entry_quantity_from_margin_power(
            broker,
            "AAPL.US",
            "BUY",
            Decimal("100"),
            "USD",
        )

        assert qty == 0
        broker.estimate_margin_max_quantity.assert_not_called()

    def test_execute_blocks_add_on_when_live_policy_disables_it(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        broker.get_positions.return_value = []
        svc.allow_position_addons = False
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("5"))

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "add-on" in status.reason
        broker.get_positions.assert_called_once_with()
        broker.submit_limit_order.assert_not_called()

    @pytest.mark.parametrize("broker_side", ["LONG", "SHORT"])
    def test_execute_blocks_broker_only_position_when_live_policy_disables_addons(
        self,
        svc: TradeExecutionService,
        broker_side: str,
    ) -> None:
        from app.core.broker import Position

        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("AAPL.US", broker_side, Decimal("5"), Decimal("100"))
        ]

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "add-on" in status.reason
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_not_called()

    def test_execute_entry_blocks_existing_cross_symbol_broker_position(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import Position

        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("MSFT.US", "LONG", Decimal("1"), Decimal("400"))
        ]

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "cross-symbol broker position MSFT.US" in status.reason
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_not_called()

    def test_final_entry_gate_catches_cross_symbol_position_appearing_after_precheck(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import Position

        class Broker:
            def __init__(self) -> None:
                self.position_reads = 0
                self.estimate_calls = 0
                self.submissions = 0

            def get_positions(self):
                self.position_reads += 1
                if self.position_reads == 1:
                    return []
                return [Position("MSFT.US", "LONG", Decimal("1"), Decimal("400"))]

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                self.estimate_calls += 1
                return Decimal("100")

            def submit_limit_order(self, *args):
                self.submissions += 1
                raise AssertionError("cross-symbol exposure must block submission")

        broker = Broker()
        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.position_reads == 2
        assert broker.estimate_calls == 0
        assert broker.submissions == 0

    def test_final_submission_gate_blocks_kill_switch_enabled_during_sizing(
        self,
        svc: TradeExecutionService,
    ) -> None:
        sizing_started = threading.Event()
        release_sizing = threading.Event()

        class Broker:
            submissions = 0

            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                sizing_started.set()
                if not release_sizing.wait(2):
                    raise TimeoutError("sizing test was not released")
                return Decimal("10")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submissions += 1
                return OrderResult("unexpected", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        risk = RiskController()
        results: list[OrderStatus | None] = []
        errors: list[BaseException] = []

        def execute_buy() -> None:
            try:
                results.append(
                    svc.execute(
                        "BUY",
                        "AAPL.US",
                        Quote("AAPL.US", 100, 99.9, 100.1, ""),
                        broker,
                        risk,
                        ServerChanNotifier(""),
                        "USD",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute_buy, daemon=True)
        worker.start()
        assert sizing_started.wait(2)
        risk.enable_kill_switch("test race")
        release_sizing.set()
        worker.join(2)

        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1
        assert results[0] is not None
        assert results[0].status == "SKIPPED"
        assert "kill switch" in results[0].reason
        assert broker.submissions == 0

    def test_final_submission_gate_blocks_pending_order_appearing_during_sizing(
        self,
        svc: TradeExecutionService,
    ) -> None:
        sizing_started = threading.Event()
        release_sizing = threading.Event()

        class Broker:
            submissions = 0

            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                sizing_started.set()
                if not release_sizing.wait(2):
                    raise TimeoutError("sizing test was not released")
                return Decimal("10")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submissions += 1
                return OrderResult("unexpected", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        results: list[OrderStatus | None] = []
        errors: list[BaseException] = []

        def execute_buy() -> None:
            try:
                results.append(
                    svc.execute(
                        "BUY",
                        "AAPL.US",
                        Quote("AAPL.US", 100, 99.9, 100.1, ""),
                        broker,
                        RiskController(),
                        ServerChanNotifier(""),
                        "USD",
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute_buy, daemon=True)
        worker.start()
        assert sizing_started.wait(2)
        svc._track_pending_order(
            "BUY",
            OrderResult(
                "racing-pending",
                "AAPL.US",
                "BUY",
                Decimal("1"),
                Decimal("100"),
                "SUBMITTED",
            ),
            broker,
            None,
        )
        release_sizing.set()
        worker.join(2)

        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1
        assert results[0] is not None
        assert results[0].status == "SKIPPED"
        assert "before submission" in results[0].reason
        assert "racing-pending" in results[0].reason
        assert broker.submissions == 0

    def test_submission_lock_serializes_until_live_order_is_tracked(
        self,
        svc: TradeExecutionService,
    ) -> None:
        first_submit_started = threading.Event()
        second_sizing_finished = threading.Event()
        release_first_submit = threading.Event()

        class Broker:
            submissions = 0
            sizing_calls = 0

            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                self.sizing_calls += 1
                if self.sizing_calls == 2:
                    second_sizing_finished.set()
                return Decimal("10")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submissions += 1
                first_submit_started.set()
                if not release_first_submit.wait(2):
                    raise TimeoutError("first submit test was not released")
                return OrderResult("first-live", symbol, side, quantity, price, "SUBMITTED")

        broker = Broker()
        results: list[OrderStatus | None] = []
        errors: list[BaseException] = []

        def execute_buy() -> None:
            try:
                results.append(
                    svc.execute(
                        "BUY",
                        "AAPL.US",
                        Quote("AAPL.US", 100, 99.9, 100.1, ""),
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
        first.start()
        assert first_submit_started.wait(2)
        second.start()
        assert not second_sizing_finished.wait(0.1)
        release_first_submit.set()
        first.join(2)
        second.join(2)

        assert not first.is_alive()
        assert not second.is_alive()
        assert errors == []
        assert broker.submissions == 1
        assert broker.sizing_calls == 1
        assert sorted(status.status for status in results if status is not None) == [
            "SKIPPED",
            "SUBMITTED",
        ]
        assert svc.pending_order_for("AAPL.US") is not None

    def test_submission_guard_serializes_external_broker_sync_with_submit(
        self,
        svc: TradeExecutionService,
    ) -> None:
        guard_acquired = threading.Event()
        release_guard = threading.Event()
        sizing_finished = threading.Event()
        submit_started = threading.Event()
        guard_errors: list[BaseException] = []
        execute_errors: list[BaseException] = []
        results: list[OrderStatus | None] = []

        class Broker:
            submissions = 0

            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, *args) -> Decimal:
                sizing_finished.set()
                return Decimal("10")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submissions += 1
                submit_started.set()
                return OrderResult("guarded-submit", symbol, side, quantity, price, "FILLED")

        broker = Broker()

        def hold_guard() -> None:
            try:
                with svc.submission_guard():
                    with svc.submission_guard():
                        guard_acquired.set()
                        if not release_guard.wait(2):
                            raise TimeoutError("submission guard test was not released")
            except BaseException as exc:
                guard_errors.append(exc)

        def execute_buy() -> None:
            try:
                results.append(
                    svc.execute(
                        "BUY",
                        "AAPL.US",
                        Quote("AAPL.US", 100, 99.9, 100.1, ""),
                        broker,
                        RiskController(),
                        ServerChanNotifier(""),
                        "USD",
                    )
                )
            except BaseException as exc:
                execute_errors.append(exc)

        guard_holder = threading.Thread(target=hold_guard, daemon=True)
        worker = threading.Thread(target=execute_buy, daemon=True)
        guard_holder.start()
        assert guard_acquired.wait(2)
        worker.start()
        try:
            assert not sizing_finished.wait(0.1)
            assert not submit_started.wait(0.1)
        finally:
            release_guard.set()
        guard_holder.join(2)
        worker.join(2)

        assert not guard_holder.is_alive()
        assert not worker.is_alive()
        assert guard_errors == []
        assert execute_errors == []
        assert len(results) == 1
        assert results[0] is not None
        assert results[0].status == "FILLED"
        assert sizing_finished.is_set()
        assert broker.submissions == 1

    def test_submission_lock_covers_immediate_fill_bookkeeping(
        self,
        svc: TradeExecutionService,
    ) -> None:
        bookkeeping_started = threading.Event()
        release_bookkeeping = threading.Event()
        guard_acquired = threading.Event()
        errors: list[BaseException] = []

        def persist_entry(*_args: object) -> None:
            bookkeeping_started.set()
            if not release_bookkeeping.wait(2):
                raise TimeoutError("immediate fill bookkeeping was not released")

        class Broker:
            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, *_args) -> Decimal:
                return Decimal("2")

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                return OrderResult(
                    "immediate-fill-lock",
                    symbol,
                    side,
                    quantity,
                    price,
                    "FILLED",
                )

        svc.margin_safety_factor = 1.0
        svc._persist_entry = persist_entry
        broker = Broker()

        def execute_buy() -> None:
            try:
                svc.execute(
                    "BUY",
                    "AAPL.US",
                    Quote("AAPL.US", 100, 99.9, 100.1, ""),
                    broker,
                    RiskController(),
                    ServerChanNotifier(""),
                    "USD",
                )
            except BaseException as exc:
                errors.append(exc)

        def acquire_guard() -> None:
            try:
                with svc.submission_guard():
                    guard_acquired.set()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute_buy, daemon=True)
        waiter = threading.Thread(target=acquire_guard, daemon=True)
        worker.start()
        assert bookkeeping_started.wait(2)
        waiter.start()
        try:
            assert not guard_acquired.wait(0.1)
        finally:
            release_bookkeeping.set()
        worker.join(2)
        waiter.join(2)

        assert not worker.is_alive()
        assert not waiter.is_alive()
        assert errors == []
        assert guard_acquired.is_set()
        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("2")

    @pytest.mark.parametrize("operation", ["reconcile", "cancel"])
    def test_pending_fill_finalization_is_serialized_with_submission_guard(
        self,
        svc: TradeExecutionService,
        operation: str,
    ) -> None:
        fill_callback_started = threading.Event()
        release_fill_callback = threading.Event()
        guard_acquired = threading.Event()
        errors: list[BaseException] = []

        def on_fill(_symbol: str, _action: str) -> None:
            fill_callback_started.set()
            if not release_fill_callback.wait(2):
                raise TimeoutError("pending fill callback was not released")

        terminal_status = SimpleNamespace(
            broker_order_id=f"{operation}-fill-lock",
            status="FILLED" if operation == "reconcile" else "CANCELLED",
            executed_quantity=Decimal("2") if operation == "reconcile" else Decimal("1"),
            executed_price=Decimal("100"),
        )

        class Broker:
            def get_order_status(self, _order_id: str):
                return terminal_status

            def cancel_order(self, _order_id: str):
                return terminal_status

        broker = Broker()
        svc._on_fill = on_fill
        svc._order_status_poll_interval_seconds = 0
        svc.load_pending_orders(
            [
                _PendingOrder(
                    broker=broker,
                    broker_order_id=terminal_status.broker_order_id,
                    symbol="AAPL.US",
                    action="BUY",
                    quantity=Decimal("2"),
                    price=Decimal("100"),
                    engine_snapshot=None,
                    next_status_check_at=0,
                )
            ]
        )

        def finalize_fill() -> None:
            try:
                if operation == "reconcile":
                    svc.reconcile()
                else:
                    svc.cancel_pending_order_for_symbol("AAPL.US")
            except BaseException as exc:
                errors.append(exc)

        def acquire_guard() -> None:
            try:
                with svc.submission_guard():
                    guard_acquired.set()
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=finalize_fill, daemon=True)
        waiter = threading.Thread(target=acquire_guard, daemon=True)
        worker.start()
        assert fill_callback_started.wait(2)
        waiter.start()
        try:
            assert not guard_acquired.wait(0.1)
        finally:
            release_fill_callback.set()
        worker.join(2)
        waiter.join(2)

        assert not worker.is_alive()
        assert not waiter.is_alive()
        assert errors == []
        assert guard_acquired.is_set()
        assert svc.pending_order_for("AAPL.US") is None

    def test_track_pending_merges_same_id_loaded_by_sync_during_submit(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.engine import EngineSnapshot, EngineState

        sync_errors: list[BaseException] = []
        sync_submitted_at = max(time.monotonic() / 2, 0.000001)
        snapshot = EngineSnapshot(
            state=EngineState.FLAT,
            last_trigger_price=100.0,
            last_trigger_at=None,
        )

        def restore_snapshot(_snapshot: EngineSnapshot) -> None:
            return None

        class Broker:
            cancel_calls = 0

            def get_positions(self):
                return []

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                sync_pending = _PendingOrder(
                    broker=self,
                    broker_order_id="sync-submit-race",
                    symbol=symbol,
                    action=side,
                    quantity=quantity,
                    price=price,
                    engine_snapshot=None,
                    avg_price=None,
                    next_status_check_at=0.0,
                    submitted_at=sync_submitted_at,
                )

                def load_from_sync() -> None:
                    try:
                        svc.load_pending_orders([sync_pending])
                    except BaseException as exc:
                        sync_errors.append(exc)

                sync_worker = threading.Thread(target=load_from_sync, daemon=True)
                sync_worker.start()
                sync_worker.join(2)
                if sync_worker.is_alive():
                    raise TimeoutError("broker sync did not finish")
                return OrderResult(
                    "sync-submit-race",
                    symbol,
                    side,
                    quantity,
                    price,
                    "SUBMITTED",
                )

            def cancel_order(self, order_id: str) -> None:
                self.cancel_calls += 1

        broker = Broker()
        risk = RiskController()
        status = svc._submit_limit_order(
            "SELL_SHORT",
            "AAPL.US",
            "SELL",
            Decimal("5"),
            Decimal("100"),
            broker,
            risk,
            ServerChanNotifier(""),
            engine_snapshot=snapshot,
            restore_engine_snapshot=restore_snapshot,
            avg_price=Decimal("99"),
        )

        assert sync_errors == []
        assert status is not None
        assert status.status == "SUBMITTED"
        assert risk.paused is False
        assert broker.cancel_calls == 0
        pending = svc.pending_order_by_broker_id("sync-submit-race")
        assert pending is not None
        assert pending.action == "SELL_SHORT"
        assert pending.engine_snapshot == snapshot
        assert pending.avg_price == Decimal("99")
        assert pending.restore_engine_snapshot_fn is restore_snapshot
        assert pending.broker is broker
        assert pending.submitted_at == sync_submitted_at

    def test_track_pending_rejects_different_id_for_same_symbol(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult(
                "first-order",
                "AAPL.US",
                "BUY",
                Decimal("1"),
                Decimal("100"),
                "SUBMITTED",
            ),
            broker,
            None,
        )

        with pytest.raises(trade_svc_module.OrderPersistenceError):
            svc._track_pending_order(
                "BUY",
                OrderResult(
                    "second-order",
                    "AAPL.US",
                    "BUY",
                    Decimal("1"),
                    Decimal("100"),
                    "SUBMITTED",
                ),
                broker,
                None,
            )

        assert svc.pending_order_ids() == ["first-order"]

    def test_execute_entry_fails_closed_when_broker_position_reader_is_missing(
        self,
        svc: TradeExecutionService,
    ) -> None:
        class BrokerWithoutPositions:
            def estimate_margin_max_quantity(self, *args) -> Decimal:
                raise AssertionError("margin estimation must not run")

            def submit_limit_order(self, *args):
                raise AssertionError("order submission must not run")

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            BrokerWithoutPositions(),
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "position lookup unavailable" in status.reason

    @pytest.mark.parametrize(
        ("attribute", "invalid_value"),
        [
            ("max_position_quantity", 0),
            ("max_position_notional", -1.0),
            ("max_position_notional", float("inf")),
            ("max_risk_per_trade", float("inf")),
            ("stop_loss_pct", 0.0),
        ],
    )
    def test_execute_entry_fails_closed_for_invalid_effective_limit(
        self,
        svc: TradeExecutionService,
        attribute: str,
        invalid_value: object,
    ) -> None:
        broker = MagicMock()
        setattr(svc, attribute, invalid_value)

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "invalid live safety limit" in status.reason
        broker.get_positions.assert_not_called()
        broker.estimate_margin_max_quantity.assert_not_called()
        broker.submit_limit_order.assert_not_called()

    def test_execute_blocks_short_entry_but_keeps_cover_path_available(
        self,
        svc: TradeExecutionService,
    ) -> None:
        svc.short_entries_enabled = False
        broker = MagicMock()

        status = svc.execute(
            "SELL_SHORT",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "short entries" in status.reason
        broker.submit_limit_order.assert_not_called()

    def test_reduce_only_rejects_position_increasing_action(
        self,
        svc: TradeExecutionService,
    ) -> None:
        broker = MagicMock()
        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            reduce_only=True,
        )
        assert status is not None
        assert status.status == "SKIPPED"
        broker.submit_limit_order.assert_not_called()

    def test_reduce_only_exit_is_not_blocked_by_cross_symbol_position(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import OrderResult, Position

        broker = MagicMock()
        broker.get_positions.return_value = [
            Position("MSFT.US", "LONG", Decimal("1"), Decimal("400")),
            Position("AAPL.US", "LONG", Decimal("2"), Decimal("100")),
        ]
        broker.submit_limit_order.return_value = OrderResult(
            "reduce-aapl",
            "AAPL.US",
            "SELL",
            Decimal("2"),
            Decimal("95"),
            "FILLED",
        )

        status = svc.execute(
            "SELL",
            "AAPL.US",
            Quote("AAPL.US", 95, 94.9, 95.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        broker.submit_limit_order.assert_called_once_with(
            "AAPL.US",
            "SELL",
            Decimal("2"),
            Decimal("95"),
        )
        assert broker.get_positions.call_count == 2

    @pytest.mark.parametrize(
        ("action", "position_side", "fresh_bbo", "expected_price"),
        [
            ("SELL", "LONG", Decimal("214.737"), Decimal("214.73")),
            (
                "BUY_TO_COVER",
                "SHORT",
                Decimal("100.123"),
                Decimal("100.13"),
            ),
        ],
    )
    def test_reduce_only_submission_binds_fresh_marketable_bbo(
        self,
        action: str,
        position_side: str,
        fresh_bbo: Decimal,
        expected_price: Decimal,
    ) -> None:
        submitted_prices: list[Decimal] = []

        class Broker:
            def get_positions(self):
                return [
                    SimpleNamespace(
                        symbol="AAPL.US",
                        side=position_side,
                        quantity=Decimal("2"),
                        available_quantity=Decimal("2"),
                        avg_price=Decimal("200"),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                submitted_prices.append(price)
                return OrderResult(
                    "fresh-bbo-exit",
                    symbol,
                    side,
                    quantity,
                    price,
                    "FILLED",
                )

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            final_order_quote_check=lambda *_args: FinalOrderQuoteCheckResult(
                executable_price=fresh_bbo
            ),
        )
        svc._record_entry_price(
            "AAPL.US",
            Decimal("200"),
            Decimal("2"),
            side=position_side,
        )

        status = svc.execute(
            action,
            "AAPL.US",
            Quote("AAPL.US", 214.9, 214.8, 215.0, ""),
            Broker(),
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        assert submitted_prices == [expected_price]

    def test_reduce_only_submission_rejects_quote_check_without_bound_price(
        self,
    ) -> None:
        broker = MagicMock()
        broker.get_positions.return_value = [
            SimpleNamespace(
                symbol="AAPL.US",
                side="LONG",
                quantity=Decimal("2"),
                available_quantity=Decimal("2"),
                avg_price=Decimal("100"),
            )
        ]
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            final_order_quote_check=lambda *_args: None,
        )

        status = svc.execute(
            "SELL",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "was not bound" in status.reason
        broker.submit_limit_order.assert_not_called()

    @pytest.mark.parametrize(
        "final_snapshot",
        [
            [],
            [
                SimpleNamespace(
                    symbol="AAPL.US",
                    side="LONG",
                    quantity=Decimal("4"),
                    available_quantity=Decimal("4"),
                    avg_price=Decimal("100"),
                )
            ],
            RuntimeError("final position lookup failed"),
        ],
        ids=["flat", "quantity-drift", "lookup-failure"],
    )
    def test_final_reduction_position_gate_pauses_and_blocks_drift(
        self,
        svc: TradeExecutionService,
        final_snapshot: object,
    ) -> None:
        initial_snapshot = [
            SimpleNamespace(
                symbol="AAPL.US",
                side="LONG",
                quantity=Decimal("5"),
                available_quantity=Decimal("5"),
                avg_price=Decimal("100"),
            )
        ]
        broker = MagicMock()
        broker.get_positions.side_effect = [initial_snapshot, final_snapshot]
        risk = RiskController()

        status = svc.execute(
            "SELL",
            "AAPL.US",
            Quote("AAPL.US", 101, 100.9, 101.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert status.reason.startswith(
            trade_svc_module.ORDER_EXECUTION_BLOCKED_PREFIX
        )
        assert risk.paused is True
        assert risk.pause_auto_resumable is False
        assert risk.pause_reason == status.reason
        assert broker.get_positions.call_count == 2
        broker.submit_limit_order.assert_not_called()

    def test_final_cover_position_gate_rechecks_short_quantity(
        self,
        svc: TradeExecutionService,
    ) -> None:
        initial_short = SimpleNamespace(
            symbol="AAPL.US",
            side="SHORT",
            quantity=Decimal("3"),
            available_quantity=Decimal("3"),
            avg_price=Decimal("100"),
        )
        reduced_short = SimpleNamespace(
            symbol="AAPL.US",
            side="SHORT",
            quantity=Decimal("2"),
            available_quantity=Decimal("2"),
            avg_price=Decimal("100"),
        )
        broker = MagicMock()
        broker.get_positions.side_effect = [[initial_short], [reduced_short]]
        risk = RiskController()

        status = svc.execute(
            "BUY_TO_COVER",
            "AAPL.US",
            Quote("AAPL.US", 99, 98.9, 99.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "BUY_TO_COVER" in status.reason
        assert risk.paused is True
        assert risk.pause_reason.startswith(
            trade_svc_module.ORDER_EXECUTION_BLOCKED_PREFIX
        )
        broker.submit_limit_order.assert_not_called()

    def test_final_submission_gate_blocks_reduce_only_exit_on_operational_pause(
        self,
        svc: TradeExecutionService,
    ) -> None:
        position_read_started = threading.Event()
        release_position_read = threading.Event()

        class Broker:
            submissions = 0

            def get_positions(self):
                position_read_started.set()
                if not release_position_read.wait(2):
                    raise TimeoutError("position lookup test was not released")
                return [
                    SimpleNamespace(
                        symbol="AAPL.US",
                        side="LONG",
                        quantity=Decimal("2"),
                        available_quantity=Decimal("2"),
                        avg_price=Decimal("100"),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submissions += 1
                return OrderResult("unexpected", symbol, side, quantity, price, "FILLED")

        broker = Broker()
        risk = RiskController()
        results: list[OrderStatus | None] = []
        errors: list[BaseException] = []

        def execute_sell() -> None:
            try:
                results.append(
                    svc.execute(
                        "SELL",
                        "AAPL.US",
                        Quote("AAPL.US", 95, 94.9, 95.1, ""),
                        broker,
                        risk,
                        ServerChanNotifier(""),
                        "USD",
                        allow_loss_exit=True,
                        reduce_only=True,
                    )
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=execute_sell, daemon=True)
        worker.start()
        assert position_read_started.wait(2)
        risk.pause(f"{trade_svc_module.ORDER_EXECUTION_BLOCKED_PREFIX} test race")
        release_position_read.set()
        worker.join(2)

        assert not worker.is_alive()
        assert errors == []
        assert len(results) == 1
        assert results[0] is not None
        assert results[0].status == "SKIPPED"
        assert "trading is paused" in results[0].reason
        assert broker.submissions == 0

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
        # Phase 2: 10-20 trades in 0.02 ticks (was 0.01 in Phase 1).
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("15.017")) == Decimal("15.000")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("15.011")) == Decimal("15.020")
        # Phase 2: 20-100 merged to 0.05 ticks (was 0.02 for 20-50 in Phase 1).
        assert TradeExecutionService._normalize_limit_price("0700.HK", "BUY", Decimal("25.037")) == Decimal("25.000")
        assert TradeExecutionService._normalize_limit_price("0700.HK", "SELL", Decimal("25.037")) == Decimal("25.050")
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
        svc.short_entries_enabled = True
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

        assert status is not None
        assert status.status == "SKIPPED"
        assert "risk caps" in status.reason
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
        svc._record_entry_price(
            "NVDA.US",
            Decimal("100"),
            Decimal("100"),
            side="SHORT",
        )
        svc._record_entry_price(
            "NVDA.US",
            Decimal("96"),
            Decimal("100"),
            side="SHORT",
        )

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
        from app.core.engine import EngineSnapshot, EngineState

        updates = []
        svc._update_order_status = lambda order_id, status, filled_at=None, executed_quantity=None, executed_price=None: updates.append((order_id, status))
        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult("order-1", "CANCELLED")
        snapshot = EngineSnapshot(state=EngineState.LONG, last_trigger_price=221.0, last_trigger_at=None)
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

    def test_mismatched_status_order_id_does_not_finalize_pending(
        self,
        svc: TradeExecutionService,
    ) -> None:
        from app.core.broker import OrderResult, OrderStatusResult
        from app.core.risk import RiskController

        broker = MagicMock()
        broker.get_order_status.return_value = OrderStatusResult(
            "another-order",
            "FILLED",
            Decimal("10"),
            Decimal("100"),
        )
        svc._order_status_poll_interval_seconds = 0
        svc._update_order_status = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult(
                "expected-order",
                "AAPL.US",
                "BUY",
                Decimal("10"),
                Decimal("100"),
                "SUBMITTED",
            ),
            broker,
            None,
        )
        risk = RiskController()

        svc.reconcile(risk=risk)

        assert svc.pending_order_for("AAPL.US") is not None
        assert svc.tracked_position("AAPL.US") is None
        assert risk.daily_pnl == 0
        svc._update_order_status.assert_not_called()

    def test_pending_orders_are_tracked_per_symbol(self, svc: TradeExecutionService) -> None:
        broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            broker,
            None,
        )
        svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "SUBMITTED"),
            broker,
            None,
        )

        assert svc.has_pending_order is True
        assert svc.pending_order is not None
        assert svc.pending_order.broker_order_id == "order-nvda"
        nvda_pending = svc.pending_order_for("NVDA.US")
        assert nvda_pending is not None
        assert nvda_pending.broker_order_id == "order-nvda"
        aapl_pending = svc.pending_order_for("AAPL.US")
        assert aapl_pending is not None
        assert aapl_pending.broker_order_id == "order-aapl"

    def test_execute_blocks_cross_symbol_entry_when_another_symbol_is_pending(self) -> None:
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
        pending_broker = MagicMock()
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            pending_broker,
            None,
        )
        broker = MagicMock()
        broker.get_cash.return_value = {"USD": Decimal("10000")}
        broker.submit_limit_order.return_value = OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "FILLED")

        status = svc.execute(
            "BUY",
            "AAPL.US",
            Quote("AAPL.US", 199, 198.9, 199.1, ""),
            broker,
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert "order-nvda" in status.reason
        assert skipped[0][3]["skip_category"] == "PENDING"
        broker.submit_limit_order.assert_not_called()

    def test_cancel_pending_order_for_symbol_leaves_other_symbols_pending(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderStatusResult

        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult("order-aapl", "CANCELLED")
        svc._track_pending_order(
            "BUY",
            OrderResult("order-nvda", "NVDA.US", "BUY", Decimal("10"), Decimal("220"), "SUBMITTED"),
            broker,
            None,
        )
        svc._track_pending_order(
            "BUY",
            OrderResult("order-aapl", "AAPL.US", "BUY", Decimal("5"), Decimal("199"), "SUBMITTED"),
            broker,
            None,
        )

        result = svc.cancel_pending_order_for_symbol("AAPL.US")

        assert result.status == "CANCELLED"
        assert svc.pending_order_for("AAPL.US") is None
        assert svc.pending_order_for("NVDA.US") is not None
        broker.cancel_order.assert_called_once_with("order-aapl")

    @pytest.mark.parametrize("cancel_status", ["SUBMITTED", "PARTIAL_FILLED", "UNKNOWN"])
    def test_cancel_pending_order_keeps_pending_until_terminal_status(
        self,
        svc: TradeExecutionService,
        cancel_status: str,
    ) -> None:
        from app.core.broker import OrderStatusResult

        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult(
            "order-aapl",
            cancel_status,
            executed_quantity=(
                Decimal("2") if cancel_status == "PARTIAL_FILLED" else None
            ),
            executed_price=(
                Decimal("100") if cancel_status == "PARTIAL_FILLED" else None
            ),
        )
        svc._track_pending_order(
            "BUY",
            OrderResult(
                "order-aapl",
                "AAPL.US",
                "BUY",
                Decimal("5"),
                Decimal("100"),
                "SUBMITTED",
            ),
            broker,
            None,
        )

        result = svc.cancel_pending_order_for_symbol("AAPL.US")

        assert result.status == cancel_status
        assert svc.pending_order_for("AAPL.US") is not None

    def test_cancel_pending_order_for_symbol_records_partial_exit_pnl_in_risk(self, svc: TradeExecutionService) -> None:
        from app.core.broker import OrderStatusResult
        from app.core.risk import RiskController

        broker = MagicMock()
        broker.cancel_order.return_value = OrderStatusResult(
            "order-sell",
            "CANCELLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("90"),
        )
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("5"))
        svc._track_pending_order(
            "SELL",
            OrderResult("order-sell", "AAPL.US", "SELL", Decimal("5"), Decimal("90"), "SUBMITTED"),
            broker,
            None,
            avg_price=Decimal("100"),
        )
        risk = RiskController()

        result = svc.cancel_pending_order_for_symbol("AAPL.US", risk=risk)

        assert result.status == "CANCELLED"
        assert risk.daily_pnl == -20.0
        assert risk.consecutive_losses == 1

    @pytest.mark.parametrize(
        ("action", "terminal_status", "expected_state", "expected_side"),
        [
            ("BUY", "CANCELLED", "LONG", "LONG"),
            ("SELL_SHORT", "REJECTED", "SHORT", "SHORT"),
        ],
    )
    def test_partial_terminal_entry_keeps_transitioned_engine_position_state(
        self,
        svc: TradeExecutionService,
        action: str,
        terminal_status: str,
        expected_state: str,
        expected_side: str,
    ) -> None:
        from app.core.broker import OrderStatusResult
        from app.core.engine import EngineState, StrategyEngine, StrategyParams

        broker = MagicMock()
        broker.get_order_status.return_value = OrderStatusResult(
            f"partial-{action.lower()}",
            terminal_status,
            executed_quantity=Decimal("2"),
            executed_price=Decimal("100"),
        )
        engine = StrategyEngine(
            StrategyParams(
                symbol="AAPL.US",
                buy_low=90,
                sell_high=110,
                short_selling=True,
            )
        )
        snapshot = engine.snapshot()
        assert engine.transition_for_action(action) == "OK"
        svc._order_status_poll_interval_seconds = 0
        svc._track_pending_order(
            action,
            OrderResult(
                f"partial-{action.lower()}",
                "AAPL.US",
                "SELL" if action == "SELL_SHORT" else "BUY",
                Decimal("5"),
                Decimal("100"),
                "SUBMITTED",
            ),
            broker,
            snapshot,
            restore_engine_snapshot_fn=engine.restore,
        )

        svc.reconcile()

        assert engine.state == EngineState[expected_state]
        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("2")
        assert tracked.side == expected_side
        assert svc.pending_order_for("AAPL.US") is None

    @pytest.mark.parametrize(
        ("action", "initial_state", "tracked_side"),
        [
            ("SELL", "LONG", "LONG"),
            ("BUY_TO_COVER", "SHORT", "SHORT"),
        ],
    )
    def test_partial_terminal_exit_restores_remaining_position_state(
        self,
        svc: TradeExecutionService,
        action: str,
        initial_state: str,
        tracked_side: str,
    ) -> None:
        from app.core.broker import OrderStatusResult
        from app.core.engine import EngineState, StrategyEngine, StrategyParams

        broker = MagicMock()
        broker.get_order_status.return_value = OrderStatusResult(
            f"partial-{action.lower()}",
            "CANCELLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("95"),
        )
        engine = StrategyEngine(
            StrategyParams(
                symbol="AAPL.US",
                buy_low=90,
                sell_high=110,
                short_selling=True,
            )
        )
        engine.state = EngineState[initial_state]
        snapshot = engine.snapshot()
        assert engine.transition_for_action(action) == "OK"
        svc._record_entry_price(
            "AAPL.US",
            Decimal("100"),
            Decimal("5"),
            side=tracked_side,
        )
        svc._order_status_poll_interval_seconds = 0
        svc._track_pending_order(
            action,
            OrderResult(
                f"partial-{action.lower()}",
                "AAPL.US",
                "BUY" if action == "BUY_TO_COVER" else "SELL",
                Decimal("5"),
                Decimal("95"),
                "SUBMITTED",
            ),
            broker,
            snapshot,
            avg_price=Decimal("100"),
            restore_engine_snapshot_fn=engine.restore,
        )

        svc.reconcile(risk=RiskController())

        assert engine.state == EngineState[initial_state]
        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")
        assert tracked.side == tracked_side
        assert svc.pending_order_for("AAPL.US") is None

    @pytest.mark.parametrize(
        (
            "action",
            "tracked_side",
            "fill_price",
            "actual_fee",
            "expected_fee",
            "expected_fee_source",
        ),
        [
            (
                "SELL",
                "LONG",
                Decimal("110"),
                Decimal("0.05"),
                0.25,
                "MIXED",
            ),
            (
                "BUY_TO_COVER",
                "SHORT",
                Decimal("90"),
                None,
                0.38,
                "ESTIMATED",
            ),
        ],
    )
    def test_exit_fill_persists_authoritative_tracked_entry_pnl_metadata(
        self,
        action: str,
        tracked_side: str,
        fill_price: Decimal,
        actual_fee: Decimal | None,
        expected_fee: float,
        expected_fee_source: str,
    ) -> None:
        updates: list[tuple[object, ...]] = []

        def update_order_status(*args: object) -> None:
            updates.append(args)

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=update_order_status,
            record_risk_event=lambda *args: None,
        )
        opened_at = datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc)
        svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("5"),
                Decimal("500"),
                tracked_side,
                opened_at,
            )
        })
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id=f"authoritative-{action.lower()}",
            symbol="AAPL.US",
            action=action,
            quantity=Decimal("2"),
            price=fill_price,
            engine_snapshot=None,
            avg_price=Decimal("80"),
            pnl_fee_rate=Decimal("0.001"),
        )
        terminal = OrderStatus(
            pending.broker_order_id,
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=fill_price,
            actual_fee=actual_fee,
            broker_updated_at=datetime(2026, 7, 15, 14, 0, tzinfo=timezone.utc),
        )

        svc._finalize_pending_fill(pending, terminal, risk=RiskController())

        assert len(updates) == 1
        order_id, status, _filled_at, executed_qty, executed_price, metadata = updates[0]
        assert order_id == pending.broker_order_id
        assert status == "FILLED"
        assert executed_qty == pytest.approx(2.0)
        assert executed_price == pytest.approx(float(fill_price))
        assert isinstance(metadata, dict)
        assert metadata["pnl_source"] == "TRACKED_ENTRY"
        assert metadata["cost_basis_price"] == pytest.approx(100.0)
        assert metadata["cost_basis_quantity"] == pytest.approx(2.0)
        assert metadata["cost_basis_opened_at"] == opened_at
        assert metadata["position_quantity_before"] == pytest.approx(5.0)
        assert metadata["gross_pnl"] == pytest.approx(20.0)
        assert metadata["pnl_fee"] == pytest.approx(expected_fee)
        assert metadata["pnl_fee_rate"] == pytest.approx(0.001)
        assert metadata["pnl_fee_source"] == expected_fee_source
        assert metadata["net_pnl"] == pytest.approx(20.0 - expected_fee)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")

    def test_same_terminal_fill_is_finalized_only_once(self) -> None:
        fills: list[str] = []
        reductions: list[tuple[str, str, Decimal]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            on_fill=lambda symbol, _action: fills.append(symbol),
            on_reduction_fill=lambda symbol, action, quantity: reductions.append(
                (symbol, action, quantity)
            ),
        )
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("5"))
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="duplicate-terminal",
            symbol="AAPL.US",
            action="SELL",
            quantity=Decimal("2"),
            price=Decimal("110"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )
        terminal = OrderStatus(
            "duplicate-terminal",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("110"),
        )
        risk = RiskController()

        svc._finalize_pending_fill(pending, terminal, risk=risk)
        svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")
        assert risk.daily_pnl == 20.0
        assert fills == ["AAPL.US"]
        assert reductions == [("AAPL.US", "SELL", Decimal("2"))]

    def test_exit_fill_emits_drawdown_limit_event_and_notification(self) -> None:
        risk_events: list[tuple[str, str]] = []
        notifications: list[tuple[str, str]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda reason, event_type="RISK_REJECTION": risk_events.append(
                (event_type, reason)
            ),
        )
        svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("1"),
                Decimal("100"),
                "LONG",
                datetime(2026, 7, 19, 13, 30, tzinfo=timezone.utc),
            )
        })
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="drawdown-fill",
            symbol="AAPL.US",
            action="SELL",
            quantity=Decimal("1"),
            price=Decimal("90"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )
        terminal = OrderStatus(
            "drawdown-fill",
            "FILLED",
            executed_quantity=Decimal("1"),
            executed_price=Decimal("90"),
        )
        risk = RiskController(
            RiskConfig(
                max_daily_loss=5000.0,
                max_consecutive_losses=10,
                max_drawdown_amount=10.0,
            )
        )

        svc._finalize_pending_fill(
            pending,
            terminal,
            risk=risk,
            notify_risk_event=lambda event_type, reason: notifications.append(
                (event_type, reason)
            ),
        )

        assert risk.paused is True
        assert risk_events == [("DRAWDOWN_LIMIT", risk.pause_reason)]
        assert notifications == [("DRAWDOWN_LIMIT", risk.pause_reason)]

    def test_concurrent_same_id_fill_finalization_skips_in_flight_reentry(self) -> None:
        callback_started = threading.Event()
        release_callback = threading.Event()
        callback_symbols: list[str] = []
        errors: list[BaseException] = []

        def block_on_fill(symbol: str, _action: str) -> None:
            callback_symbols.append(symbol)
            callback_started.set()
            if not release_callback.wait(2):
                raise TimeoutError("fill callback test was not released")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            on_fill=block_on_fill,
        )
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="concurrent-terminal",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("2"),
            price=Decimal("100"),
            engine_snapshot=None,
        )
        terminal = OrderStatus(
            "concurrent-terminal",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("100"),
        )

        def finalize_first() -> None:
            try:
                svc._finalize_pending_fill(pending, terminal)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=finalize_first, daemon=True)
        worker.start()
        assert callback_started.wait(2)
        try:
            svc._finalize_pending_fill(pending, terminal)
        finally:
            release_callback.set()
        worker.join(2)
        svc._finalize_pending_fill(pending, terminal)

        assert not worker.is_alive()
        assert errors == []
        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("2")
        assert callback_symbols == ["AAPL.US"]

    def test_failed_fill_finalization_can_be_retried(self) -> None:
        class FlakyRisk(RiskController):
            def __init__(self) -> None:
                super().__init__()
                self.record_calls = 0

            def record_trade(self, pnl: float) -> None:
                self.record_calls += 1
                if self.record_calls == 1:
                    raise RuntimeError("temporary risk persistence failure")
                super().record_trade(pnl)

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )
        svc._record_entry_price("AAPL.US", Decimal("100"), Decimal("5"))
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="retry-terminal",
            symbol="AAPL.US",
            action="SELL",
            quantity=Decimal("2"),
            price=Decimal("110"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )
        terminal = OrderStatus(
            "retry-terminal",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("110"),
        )
        risk = FlakyRisk()

        with pytest.raises(RuntimeError, match="temporary risk persistence failure"):
            svc._finalize_pending_fill(pending, terminal, risk=risk)
        tracked_after_failure = svc.tracked_position("AAPL.US")
        assert tracked_after_failure is not None
        assert tracked_after_failure.quantity == Decimal("5")

        svc._finalize_pending_fill(pending, terminal, risk=risk)
        svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked_after_retry = svc.tracked_position("AAPL.US")
        assert tracked_after_retry is not None
        assert tracked_after_retry.quantity == Decimal("3")
        assert risk.record_calls == 2
        assert risk.daily_pnl == 20.0

    def test_authoritative_accounting_write_failure_has_no_memory_side_effects(
        self,
    ) -> None:
        should_fail = True
        tracked_writes: list[tuple[str, Decimal, Decimal]] = []
        fill_callbacks: list[str] = []

        def update_order_status(*_args: object) -> None:
            if should_fail:
                raise RuntimeError("temporary ledger failure")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=update_order_status,
            record_risk_event=lambda *args: None,
            persist_entry=lambda symbol, quantity, cost: tracked_writes.append(
                (symbol, quantity, cost)
            ),
            on_fill=lambda symbol, _action: fill_callbacks.append(symbol),
        )
        svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("5"),
                Decimal("500"),
                "LONG",
                datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
            )
        })
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="accounting-retry",
            symbol="AAPL.US",
            action="SELL",
            quantity=Decimal("2"),
            price=Decimal("110"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )
        terminal = OrderStatus(
            "accounting-retry",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("110"),
        )
        risk = RiskController()

        with pytest.raises(OrderPersistenceError, match="authoritative accounting"):
            svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("5")
        assert tracked.cost == Decimal("500")
        assert tracked_writes == []
        assert fill_callbacks == []
        assert risk.daily_pnl == 0.0
        assert risk.paused is True
        assert svc.pending_order_ids() == ["accounting-retry"]

        should_fail = False
        svc._finalize_pending_fill(pending, terminal, risk=risk)
        svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")
        assert tracked.cost == Decimal("300")
        assert tracked_writes == [("AAPL.US", Decimal("3"), Decimal("300"))]
        assert fill_callbacks == ["AAPL.US"]
        assert risk.daily_pnl == 20.0

    def test_tracked_reduction_write_failure_is_retryable_without_double_pnl(
        self,
    ) -> None:
        persist_calls = 0

        def persist_entry(_symbol: str, _quantity: Decimal, _cost: Decimal) -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise RuntimeError("temporary tracked-entry failure")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            persist_entry=persist_entry,
        )
        svc.load_tracked_entries({
            "AAPL.US": (
                Decimal("5"),
                Decimal("500"),
                "LONG",
                datetime(2026, 7, 15, 13, 30, tzinfo=timezone.utc),
            )
        })
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="tracked-retry",
            symbol="AAPL.US",
            action="SELL",
            quantity=Decimal("2"),
            price=Decimal("110"),
            engine_snapshot=None,
            avg_price=Decimal("100"),
        )
        terminal = OrderStatus(
            "tracked-retry",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("110"),
        )
        risk = RiskController()

        with pytest.raises(OrderPersistenceError, match="tracked reduction"):
            svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("5")
        assert risk.daily_pnl == 0.0

        svc._finalize_pending_fill(pending, terminal, risk=risk)
        svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("3")
        assert persist_calls == 2
        assert risk.daily_pnl == 20.0

    def test_tracked_entry_write_failure_keeps_entry_fill_retryable(self) -> None:
        persist_calls = 0
        fill_callbacks: list[str] = []

        def persist_entry(_symbol: str, _quantity: Decimal, _cost: Decimal) -> None:
            nonlocal persist_calls
            persist_calls += 1
            if persist_calls == 1:
                raise RuntimeError("temporary tracked-entry failure")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            persist_entry=persist_entry,
            on_fill=lambda symbol, _action: fill_callbacks.append(symbol),
        )
        pending = _PendingOrder(
            broker=MagicMock(),
            broker_order_id="entry-retry",
            symbol="AAPL.US",
            action="BUY",
            quantity=Decimal("2"),
            price=Decimal("100"),
            engine_snapshot=None,
        )
        terminal = OrderStatus(
            "entry-retry",
            "FILLED",
            executed_quantity=Decimal("2"),
            executed_price=Decimal("100"),
        )
        risk = RiskController()

        with pytest.raises(OrderPersistenceError, match="tracked entry"):
            svc._finalize_pending_fill(pending, terminal, risk=risk)

        assert svc.tracked_position("AAPL.US") is None
        assert fill_callbacks == []
        assert risk.paused is True
        assert svc.pending_order_ids() == ["entry-retry"]

        svc._finalize_pending_fill(pending, terminal, risk=risk)
        svc._finalize_pending_fill(pending, terminal, risk=risk)

        tracked = svc.tracked_position("AAPL.US")
        assert tracked is not None
        assert tracked.quantity == Decimal("2")
        assert tracked.cost == Decimal("200")
        assert persist_calls == 2
        assert fill_callbacks == ["AAPL.US"]

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

    def test_paused_risk_allows_position_reducing_sell(self) -> None:
        class Broker:
            def get_positions(self):
                return [SimpleNamespace(symbol="NVDA.US", side="LONG", quantity=Decimal("10"), avg_price=Decimal("220"))]

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal):
                return OrderResult("order-exit", symbol, side, quantity, price, "FILLED")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )
        risk = RiskController()
        risk.pause("pending order order-entry timed out after 30s")

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            Broker(),
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
        )

        assert status is not None
        assert status.status == "FILLED"

    def test_verified_protective_permission_allows_operational_pause_exit(self) -> None:
        class Broker:
            submitted_quantity = Decimal("0")

            def get_positions(self):
                return [
                    SimpleNamespace(
                        symbol="NVDA.US",
                        side="LONG",
                        quantity=Decimal("10"),
                        available_quantity=Decimal("10"),
                        avg_price=Decimal("220"),
                    )
                ]

            def submit_limit_order(
                self,
                symbol: str,
                side: str,
                quantity: Decimal,
                price: Decimal,
            ) -> OrderResult:
                self.submitted_quantity = quantity
                return OrderResult("order-protective", symbol, side, quantity, price, "FILLED")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            final_order_quote_check=lambda _broker, _symbol, _action, price: (
                FinalOrderQuoteCheckResult(executable_price=price)
            ),
        )
        risk = RiskController()
        risk.pause(
            f"{trade_svc_module.PNL_RECONCILIATION_UNCERTAIN_PREFIX} verified"
        )
        assert risk.permit_protective_exits() is True

        broker = Broker()
        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "FILLED"
        assert broker.submitted_quantity == Decimal("10")

    def test_unarmed_pnl_pause_blocks_position_reducing_order(self) -> None:
        class Broker:
            @staticmethod
            def get_positions() -> list[object]:
                raise AssertionError("unarmed PnL pause must fail before broker lookup")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )
        risk = RiskController()
        risk.pause(
            f"{trade_svc_module.PNL_RECONCILIATION_UNCERTAIN_PREFIX} unverified"
        )

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            Broker(),
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert status.reason.startswith("trading is paused:")

    def test_revoked_protective_permission_blocks_final_submission(self) -> None:
        risk = RiskController()
        risk.pause(
            f"{trade_svc_module.PNL_RECONCILIATION_UNCERTAIN_PREFIX} verified"
        )
        assert risk.permit_protective_exits() is True

        class Broker:
            submitted = False

            def get_positions(self):
                risk.revoke_protective_exits()
                return [
                    SimpleNamespace(
                        symbol="NVDA.US",
                        side="LONG",
                        quantity=Decimal("10"),
                        available_quantity=Decimal("10"),
                        avg_price=Decimal("220"),
                    )
                ]

            def submit_limit_order(self, *args: object, **kwargs: object) -> OrderResult:
                self.submitted = True
                raise AssertionError("revoked permission must block broker submission")

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
        )
        broker = Broker()

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submitted is False

    def test_quote_check_revocation_blocks_final_protective_submission(self) -> None:
        risk = RiskController()
        risk.pause(f"{trade_svc_module.ORDER_EXECUTION_BLOCKED_PREFIX} verified")
        assert risk.permit_protective_exits() is True

        class Broker:
            submitted = False

            def get_positions(self):
                return [
                    SimpleNamespace(
                        symbol="NVDA.US",
                        side="LONG",
                        quantity=Decimal("10"),
                        available_quantity=Decimal("10"),
                        avg_price=Decimal("220"),
                    )
                ]

            def submit_limit_order(self, *args: object, **kwargs: object) -> OrderResult:
                self.submitted = True
                raise AssertionError("revoked permission must block broker submission")

        def revoke_during_quote_check(*args: object) -> None:
            risk.revoke_protective_exits()

        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            final_order_quote_check=revoke_during_quote_check,
        )
        broker = Broker()

        status = svc.execute(
            "SELL",
            "NVDA.US",
            Quote("NVDA.US", 215, 214.9, 215.1, ""),
            broker,
            risk,
            ServerChanNotifier(""),
            "USD",
            allow_loss_exit=True,
            reduce_only=True,
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert broker.submitted is False

    def test_opening_warmup_blocks_new_entry_orders(self, monkeypatch) -> None:
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
        )
        monkeypatch.setattr(trade_svc_module, "is_trading_hours", lambda market: True)
        monkeypatch.setattr(trade_svc_module, "is_opening_warmup", lambda market, minutes: True, raising=False)

        class Broker:
            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal):
                return OrderResult("order-entry", symbol, side, quantity, price, "FILLED")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 197, 196.9, 197.1, ""),
            Broker(),
            RiskController(),
            ServerChanNotifier(""),
            "USD",
            trading_session_mode="RTH_ONLY",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "SESSION"
        assert "opening warmup" in skipped[0][2]

    def test_buy_add_on_is_skipped_when_existing_long_is_losing(self) -> None:
        skipped: list[tuple[str, str, str, dict[str, object]]] = []
        svc = TradeExecutionService(
            record_order=lambda *args: None,
            update_order_status=lambda *args: None,
            record_risk_event=lambda *args: None,
            record_order_skipped=lambda symbol, action, reason, payload: skipped.append((symbol, action, reason, payload)),
            allow_position_addons=True,
        )
        svc.load_tracked_entries({"NVDA.US": (Decimal("100"), Decimal("20000"))})

        class Broker:
            def get_positions(self):
                return []

            def estimate_margin_max_quantity(self, symbol: str, side: str, price: Decimal, currency=None) -> Decimal:
                return Decimal("10")

            def submit_limit_order(self, symbol: str, side: str, quantity: Decimal, price: Decimal):
                return OrderResult("order-add-on", symbol, side, quantity, price, "FILLED")

        status = svc.execute(
            "BUY",
            "NVDA.US",
            Quote("NVDA.US", 193, 192.9, 193.1, ""),
            Broker(),
            RiskController(),
            ServerChanNotifier(""),
            "USD",
        )

        assert status is not None
        assert status.status == "SKIPPED"
        assert skipped[0][3]["skip_category"] == "POSITION"
        assert "losing long" in skipped[0][2]

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


class TestOrderStatusNoneSafety:
    """Regression tests for the broker-doesn't-report-fill path.

    Before the fix, ``_coerce_order_status`` returned ``None`` for the
    executed_* fields and consumers did ``if x.executed_quantity > 0``,
    which raised ``TypeError`` against ``None``."""

    def test_coerce_status_with_missing_fills_returns_none(self) -> None:
        class FakeResult:
            broker_order_id = "x"
            status = "CANCELLED"
        s = TradeExecutionService._coerce_order_status(FakeResult(), "x")
        assert s.executed_quantity is None
        assert s.executed_price is None

    def test_positive_helper_handles_none(self) -> None:
        assert OrderStatus._positive(None) == Decimal("0")
        assert OrderStatus._positive(Decimal("5")) == Decimal("5")
        assert OrderStatus._positive(Decimal("0")) == Decimal("0")
        assert OrderStatus._positive(Decimal("-1")) == Decimal("0")

    def test_compare_via_positive_does_not_raise(self) -> None:
        class FakeResult:
            broker_order_id = "x"
            status = "CANCELLED"
        s = TradeExecutionService._coerce_order_status(FakeResult(), "x")
        # Old form would raise TypeError; new form must not.
        if OrderStatus._positive(s.executed_quantity) > 0:
            pass
