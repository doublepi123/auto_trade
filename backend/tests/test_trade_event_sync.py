# pyright: reportArgumentType=false, reportAttributeAccessIssue=false
from __future__ import annotations

from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
import pytest

from app.core.broker import BrokerOrder
from app.core.engine import StrategyParams
from app.core.market_calendar import trade_day_for
from app.database import SessionLocal
from app.models import OrderRecord, TradeEvent
from app.runner import AppRunner
from app import database

database.init_db()


def _clean() -> None:
    db = SessionLocal()
    try:
        db.query(TradeEvent).delete()
        db.query(OrderRecord).delete()
        db.commit()
    finally:
        db.close()


class TestTradeEventSync:
    def test_record_order_skipped_writes_trade_event(self) -> None:
        _clean()
        runner = AppRunner()

        runner._record_order_skipped(
            "NVDA.US",
            "SELL",
            "expected profit 4.00 is below required minimum profit 5.00",
            {"skip_category": "FEE", "expected_profit": 4.0, "required_profit": 5.0},
        )

        db = SessionLocal()
        try:
            event = db.query(TradeEvent).one()
            assert event.event_type == "ORDER_SKIPPED"
            assert event.symbol == "NVDA.US"
            assert event.side == "SELL"
            assert event.status == "SKIPPED"
            assert "expected profit" in event.message
            assert "expected_profit" in event.payload_json
            assert '"skip_category": "FEE"' in event.payload_json
        finally:
            db.close()

    def test_sync_today_orders_upserts_external_order_and_records_event(self) -> None:
        _clean()
        created_at = datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc)

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="manual-1",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("10"),
                        price=Decimal("220.10"),
                        executed_quantity=Decimal("0"),
                        executed_price=Decimal("0"),
                        status="SUBMITTED",
                        created_at=created_at,
                        filled_at=None,
                    ),
                ]

        runner = AppRunner()
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker(force=True) == 1

        db = SessionLocal()
        try:
            order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == "manual-1").one()
            assert order.symbol == "NVDA.US"
            assert order.side == "BUY"
            assert order.status == "SUBMITTED"

            event = db.query(TradeEvent).filter(TradeEvent.broker_order_id == "manual-1").one()
            assert event.event_type == "ORDER_SYNCED"
            assert event.symbol == "NVDA.US"
            assert event.status == "SUBMITTED"
        finally:
            db.close()

    def test_terminal_sync_replaces_pre_execution_timestamp(self) -> None:
        _clean()
        submitted_at = datetime.now(timezone.utc) - timedelta(minutes=3)
        executed_at = submitted_at + timedelta(minutes=2)
        with SessionLocal() as db:
            db.add(
                OrderRecord(
                    broker_order_id="late-fill",
                    symbol="NVDA.US",
                    side="BUY",
                    quantity=10.0,
                    price=220.0,
                    executed_quantity=None,
                    executed_price=None,
                    status="SUBMITTED",
                    created_at=submitted_at,
                    # Simulates the legacy parser bug that treated updated_at
                    # from an unfilled order as its fill timestamp.
                    filled_at=submitted_at,
                )
            )
            db.commit()

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="late-fill",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("10"),
                        price=Decimal("220"),
                        executed_quantity=Decimal("10"),
                        executed_price=Decimal("221"),
                        status="FILLED",
                        created_at=submitted_at,
                        filled_at=executed_at,
                    )
                ]

        runner = AppRunner()
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker(force=True) == 1

        with SessionLocal() as db:
            order = (
                db.query(OrderRecord)
                .filter(OrderRecord.broker_order_id == "late-fill")
                .one()
            )
            normalized = order.filled_at
            assert normalized is not None
            if normalized.tzinfo is None:
                normalized = normalized.replace(tzinfo=timezone.utc)
            assert normalized == executed_at

    def test_periodic_sync_latches_non_primary_live_order_globally(self) -> None:
        _clean()
        created_at = datetime.now(timezone.utc)

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="external-nvda-1",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("2"),
                        price=Decimal("220"),
                        executed_quantity=Decimal("0"),
                        executed_price=Decimal("0"),
                        status="SUBMITTED",
                        created_at=created_at,
                        filled_at=None,
                    )
                ]

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker() == 1

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
        assert "NVDA.US=[external-nvda-1]" in runner.risk.pause_reason
        assert runner.risk.check().approved is False
        # Broker-only orders are globally latched but never promoted into a
        # manageable pending order without local submission provenance.
        assert runner._trade_svc.pending_order_ids() == []
        assert runner.diagnostics()["pending_order_ids"] == ["external-nvda-1"]

    def test_periodic_sync_preserves_and_latches_two_live_ids_for_same_symbol(self) -> None:
        _clean()
        created_at = datetime.now(timezone.utc)

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id=order_id,
                        symbol="AAPL.US",
                        side="BUY",
                        quantity=Decimal("2"),
                        price=Decimal("100"),
                        executed_quantity=Decimal("0"),
                        executed_price=Decimal("0"),
                        status="SUBMITTED",
                        created_at=created_at,
                        filled_at=None,
                    )
                    for order_id in ("double-live-1", "double-live-2")
                ]

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker() == 2

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
        assert "double-live-1" in runner.risk.pause_reason
        assert "double-live-2" in runner.risk.pause_reason
        assert runner._trade_svc.pending_order_ids() == []
        assert runner._trade_svc.pending_order_inventory() == {}
        assert runner.diagnostics()["pending_order_ids"] == [
            "double-live-1",
            "double-live-2",
        ]

    def test_sync_latches_live_order_that_cannot_be_represented(self) -> None:
        _clean()

        class Broker:
            def get_today_orders(self) -> list[object]:
                return [
                    SimpleNamespace(
                        broker_order_id="bad-live-1",
                        symbol="AAPL.US",
                        side="BUY",
                        quantity=0,
                        price=float("nan"),
                        executed_quantity=0,
                        executed_price=0,
                        status="SUBMITTED",
                    )
                ]

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker() == 0

        assert runner.risk.paused is True
        assert runner.risk.pause_reason.startswith("ORDER_RECONCILIATION_UNCERTAIN:")
        assert "bad-live-1" in runner.risk.pause_reason
        assert "invalid quantity" in runner.risk.pause_reason
        assert "invalid price" in runner.risk.pause_reason
        diagnostics = runner.diagnostics()
        assert diagnostics["pending_order_ids"] == ["bad-live-1"]
        assert diagnostics["unrepresentable_live_order_issues"]

    def test_sync_latches_unrepresentable_live_row_already_in_database(self) -> None:
        _clean()
        with SessionLocal() as db:
            db.add(
                OrderRecord(
                    broker_order_id="db-bad-live-1",
                    symbol="AAPL.US",
                    side="BUY",
                    quantity=0,
                    price=100,
                    status="SUBMITTED",
                )
            )
            db.commit()

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return []

        runner = AppRunner()
        runner.engine.params = StrategyParams(symbol="AAPL.US", market="US")
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker() == 0

        assert runner.risk.paused is True
        assert "db-bad-live-1" in runner.risk.pause_reason
        assert "invalid quantity" in runner.risk.pause_reason
        assert runner._trade_svc.pending_order_ids() == []
        assert runner.diagnostics()["pending_order_ids"] == ["db-bad-live-1"]

    def test_sync_today_orders_records_terminal_status_change_event(self) -> None:
        _clean()
        db = SessionLocal()
        try:
            db.add(
                OrderRecord(
                    broker_order_id="order-filled",
                    symbol="NVDA.US",
                    side="SELL",
                    quantity=2,
                    price=221.0,
                    status="SUBMITTED",
                )
            )
            db.commit()
        finally:
            db.close()

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="order-filled",
                        symbol="NVDA.US",
                        side="SELL",
                        quantity=Decimal("2"),
                        price=Decimal("221.00"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("221.50"),
                        status="FILLED",
                        created_at=datetime(2026, 5, 22, 13, 0, tzinfo=timezone.utc),
                        filled_at=datetime(2026, 5, 22, 13, 1, tzinfo=timezone.utc),
                    ),
                ]

        runner = AppRunner()
        runner.broker = Broker()

        assert runner.sync_today_orders_from_broker(force=True) == 1

        db = SessionLocal()
        try:
            order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == "order-filled").one()
            assert order.status == "FILLED"
            assert order.executed_quantity == 2
            assert order.executed_price == 221.5
            assert order.filled_at is not None

            event = db.query(TradeEvent).filter(TradeEvent.broker_order_id == "order-filled").one()
            assert event.event_type == "ORDER_FILLED"
            assert event.side == "SELL"
            assert event.status == "FILLED"
        finally:
            db.close()

    def test_sync_today_orders_recomputes_realized_daily_pnl(self) -> None:
        _clean()
        trade_day = trade_day_for("US")
        buy_fill = datetime.combine(trade_day, time(14, 0), tzinfo=timezone.utc)
        sell_fill = datetime.combine(trade_day, time(14, 5), tzinfo=timezone.utc)

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="pnl-buy",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("2"),
                        price=Decimal("100"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("100"),
                        status="FILLED",
                        created_at=buy_fill,
                        filled_at=buy_fill,
                    ),
                    BrokerOrder(
                        broker_order_id="pnl-sell",
                        symbol="NVDA.US",
                        side="SELL",
                        quantity=Decimal("2"),
                        price=Decimal("103"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("103"),
                        status="FILLED",
                        created_at=sell_fill,
                        filled_at=sell_fill,
                    ),
                ]

        runner = AppRunner()
        runner.broker = Broker()
        runner.risk.daily_pnl = 999.0
        runner.risk.consecutive_losses = 3

        assert runner.sync_today_orders_from_broker(force=True) == 2

        assert runner.risk.daily_pnl == pytest.approx(5.797)
        assert runner.risk.consecutive_losses == 0

    def test_sync_today_orders_keeps_less_optimistic_live_pnl(self) -> None:
        _clean()
        trade_day = trade_day_for("US")
        historical_fill = datetime.combine(
            trade_day - timedelta(days=1),
            time(14, 0),
            tzinfo=timezone.utc,
        )
        buy_fill = datetime.combine(trade_day, time(14, 0), tzinfo=timezone.utc)
        sell_fill = datetime.combine(trade_day, time(14, 5), tzinfo=timezone.utc)

        db = SessionLocal()
        try:
            db.add(OrderRecord(
                broker_order_id="phantom-historical-buy",
                symbol="NVDA.US",
                side="BUY",
                quantity=1,
                price=50,
                executed_quantity=1,
                executed_price=50,
                status="FILLED",
                created_at=historical_fill,
                filled_at=historical_fill,
            ))
            db.commit()
        finally:
            db.close()

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="live-pnl-buy",
                        symbol="NVDA.US",
                        side="BUY",
                        quantity=Decimal("2"),
                        price=Decimal("100"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("100"),
                        status="FILLED",
                        created_at=buy_fill,
                        filled_at=buy_fill,
                    ),
                    BrokerOrder(
                        broker_order_id="live-pnl-sell",
                        symbol="NVDA.US",
                        side="SELL",
                        quantity=Decimal("2"),
                        price=Decimal("103"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("103"),
                        status="FILLED",
                        created_at=sell_fill,
                        filled_at=sell_fill,
                    ),
                ]

        runner = AppRunner()
        runner.broker = Broker()
        runner.risk.replace_daily_pnl(6.0, 0, trade_day)

        assert runner.sync_today_orders_from_broker(force=True) == 2

        assert runner.risk.daily_pnl == 6.0
        assert runner.risk.consecutive_losses == 0
        assert runner._sync_risk_from_order_ledger() is False

    def test_sync_today_orders_does_not_clear_losses_when_ledger_has_unmatched_exit(self) -> None:
        _clean()
        trade_day = trade_day_for("US")
        sell_fill = datetime.combine(trade_day, time(14, 5), tzinfo=timezone.utc)

        class Broker:
            def get_today_orders(self) -> list[BrokerOrder]:
                return [
                    BrokerOrder(
                        broker_order_id="unmatched-loss-sell",
                        symbol="NVDA.US",
                        side="SELL",
                        quantity=Decimal("2"),
                        price=Decimal("90"),
                        executed_quantity=Decimal("2"),
                        executed_price=Decimal("90"),
                        status="FILLED",
                        created_at=sell_fill,
                        filled_at=sell_fill,
                    ),
                ]

        runner = AppRunner()
        runner.broker = Broker()
        runner.risk.daily_pnl = -100.0
        runner.risk.consecutive_losses = 2

        assert runner.sync_today_orders_from_broker(force=True) == 1

        assert runner.risk.daily_pnl == -100.0
        assert runner.risk.consecutive_losses == 2
