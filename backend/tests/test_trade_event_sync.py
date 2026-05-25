from __future__ import annotations

from datetime import datetime, time, timezone
from decimal import Decimal

from app.core.broker import BrokerOrder
from app.core.market_calendar import trade_day_for
from app.database import SessionLocal, engine as db_engine
from app.models import Base, OrderRecord, TradeEvent
from app.runner import AppRunner


Base.metadata.create_all(bind=db_engine)


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
            {"expected_profit": 4.0, "required_profit": 5.0},
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

        assert runner.risk.daily_pnl == 6.0
        assert runner.risk.consecutive_losses == 0
