from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from app.database import engine
from app.models import Base, PaperOrder
from app.platform.paper_broker import PaperBroker
from app.platform.sdk import OrderIntent
from sqlalchemy.orm import Session


def _setup():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)


def test_submit_persists_order_row():
    _setup()
    with Session(engine) as db:
        broker = PaperBroker(session=db)
        intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t")
        ev = broker.submit(intent)
        rows = db.query(PaperOrder).filter_by(broker_order_id=ev.broker_order_id).all()
        assert len(rows) == 1
        assert rows[0].status == "SUBMITTED"
        assert rows[0].quantity == 10


def test_fill_updates_persisted_quantity():
    _setup()
    with Session(engine) as db:
        broker = PaperBroker(session=db)
        intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t")
        ev = broker.submit(intent)
        from app.platform.events import BarEvent, EventSource
        bar = BarEvent(
            timestamp=datetime(2026, 6, 22, 10, 1, tzinfo=timezone.utc),
            source=EventSource.MARKET, symbol="AAPL.US",
            open=Decimal("146"), high=Decimal("146"), low=Decimal("144"), close=Decimal("144.5"), volume=10000,
        )
        broker.on_bar(bar)
        row = db.query(PaperOrder).filter_by(broker_order_id=ev.broker_order_id).first()
        assert row is not None
        assert row.filled_quantity == 10
        assert row.status == "FILLED"


def test_cancel_persists_status():
    _setup()
    with Session(engine) as db:
        broker = PaperBroker(session=db)
        intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t")
        ev = broker.submit(intent)
        broker.cancel(ev.broker_order_id)
        row = db.query(PaperOrder).filter_by(broker_order_id=ev.broker_order_id).first()
        assert row is not None
        assert row.status == "CANCELLED"


def test_from_db_reloads_open_orders():
    _setup()
    with Session(engine) as db:
        broker = PaperBroker(session=db)
        intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=10, order_type="LIMIT", limit_price=Decimal("145"), reason="t")
        ev = broker.submit(intent)
        order_id = ev.broker_order_id
    # new broker instance reloads
    with Session(engine) as db2:
        reloaded = PaperBroker.from_db(db2)
        assert order_id in reloaded._orders
        assert reloaded._orders[order_id].status == "SUBMITTED"
        assert reloaded._orders[order_id].intent.quantity == 10


def test_no_session_still_works_in_memory():
    broker = PaperBroker()
    intent = OrderIntent(symbol="AAPL.US", side="BUY", quantity=5, order_type="LIMIT", limit_price=Decimal("145"), reason="t")
    ev = broker.submit(intent)
    assert ev.status == "SUBMITTED"
