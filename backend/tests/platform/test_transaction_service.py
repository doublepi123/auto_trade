from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy.orm import Session

from app.database import engine
from app.models import Base
from app.platform.bus import EventBus
from app.platform.events import EventSource, FillEvent
from app.platform.transaction_service import TransactionLogger, TransactionService


def _setup() -> None:
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def _fill(symbol: str, side: str, qty: int, price: str, minute: int) -> FillEvent:
    return FillEvent(
        timestamp=datetime(2026, 6, 23, 10, minute, tzinfo=timezone.utc),
        source=EventSource.BROKER,
        symbol=symbol,
        broker_order_id=f"o-{symbol}-{minute}",
        side=side,
        quantity=qty,
        price=Decimal(price),
        commission=Decimal("0.5"),
    )


def test_record_and_list() -> None:
    _setup()
    with Session(engine) as db:
        svc = TransactionService(db=db)
        svc.record(_fill("A", "BUY", 10, "100", 0))
        svc.record(_fill("A", "SELL", 10, "110", 1))
        rows = svc.list()
    assert len(rows) == 2
    assert rows[0]["side"] in ("BUY", "SELL")
    assert all(r["symbol"] == "A" for r in rows)


def test_list_filters_by_symbol() -> None:
    _setup()
    with Session(engine) as db:
        svc = TransactionService(db=db)
        svc.record(_fill("A", "BUY", 1, "100", 0))
        svc.record(_fill("B", "BUY", 1, "200", 0))
        rows = svc.list(symbol="A")
    assert len(rows) == 1 and rows[0]["symbol"] == "A"


def test_logger_subscribes_to_bus_and_records() -> None:
    _setup()
    bus = EventBus()
    with Session(engine) as db:
        logger = TransactionLogger(service=TransactionService(db=db))
        logger.subscribe(bus)
        bus.publish(_fill("A", "BUY", 5, "100", 0))
    with Session(engine) as db:
        rows = TransactionService(db=db).list()
    assert len(rows) == 1
    assert rows[0]["quantity"] == 5
