from __future__ import annotations

from app.database import engine, init_db
from app.models import Base, PaperOrder
from sqlalchemy.orm import Session


def test_paper_order_roundtrip():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with Session(engine) as db:
        order = PaperOrder(
            broker_order_id="paper-abc12345",
            symbol="AAPL.US",
            side="BUY",
            quantity=100,
            filled_quantity=0,
            limit_price=145.0,
            status="SUBMITTED",
            intent_json='{"symbol":"AAPL.US"}',
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        assert order.id is not None
        assert order.broker_order_id == "paper-abc12345"
        assert order.filled_quantity == 0


def test_paper_order_table_ensured():
    Base.metadata.drop_all(bind=engine)
    init_db()
    with Session(engine) as db:
        order = PaperOrder(
            broker_order_id="paper-initdb",
            symbol="TSLA.US",
            side="SELL",
            quantity=10,
        )
        db.add(order)
        db.commit()
        fetched = db.query(PaperOrder).filter_by(broker_order_id="paper-initdb").first()
        assert fetched is not None
        assert fetched.status == "SUBMITTED"
