from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.core.broker import BrokerOrder
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
