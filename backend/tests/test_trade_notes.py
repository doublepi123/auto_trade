"""Trade Journal (trade_notes) — service + API. Per-file sqlite, no real DB."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_trade_notes_{os.getpid()}.db"
)

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base, OrderRecord, TradeNote
from app.schemas import TradeNoteUpsert
from app.services.trade_note_service import (
    OrderNotFoundError,
    TradeNoteNotFoundError,
    TradeNoteService,
)


class _Base:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            os.environ["AUTO_TRADE_DATABASE_URL"], connect_args={"check_same_thread": False}
        )
        Base.metadata.drop_all(bind=cls.engine)
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            db = Session(bind=cls.engine)
            try:
                yield db
            finally:
                db.close()

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)

    def setup_method(self) -> None:
        # Tests in one class share the DB; clear notes + orders before each method.
        self._order_sequence = 0
        db = self._db()
        db.query(TradeNote).delete()
        db.query(OrderRecord).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _make_order(self, symbol: str = "AAPL.US", side: str = "BUY") -> int:
        self._order_sequence += 1
        db = self._db()
        order = OrderRecord(
            broker_order_id=f"O-{symbol}-{side}-{self._order_sequence}",
            symbol=symbol,
            side=side,
            quantity=10,
            price=100.0,
        )
        db.add(order)
        db.commit()
        db.refresh(order)
        oid = order.id
        db.close()
        return oid


class TestTradeNoteService(_Base):
    def test_upsert_creates_then_updates(self) -> None:
        oid = self._make_order()
        svc = TradeNoteService(self._db())
        created = svc.upsert_note(oid, TradeNoteUpsert(note="first", tags=["good"], rating=4))
        assert created.order_id == oid
        assert created.symbol == "AAPL.US"
        assert created.note == "first"
        assert created.tags == ["good"]
        assert created.rating == 4

        updated = svc.upsert_note(oid, TradeNoteUpsert(note="second", tags=[], rating=None))
        assert updated.note == "second"
        assert updated.tags == []
        assert updated.rating is None
        # Still one row (upsert, not insert).
        page = svc.list_notes()
        assert page.total == 1

    def test_get_missing_raises(self) -> None:
        svc = TradeNoteService(self._db())
        with pytest.raises(TradeNoteNotFoundError):
            svc.get_note(999999)

    def test_upsert_missing_order_raises(self) -> None:
        svc = TradeNoteService(self._db())
        with pytest.raises(OrderNotFoundError):
            svc.upsert_note(999999, TradeNoteUpsert(note="x"))

    def test_list_filters_by_symbol_and_paginates(self) -> None:
        a1 = self._make_order("AAPL.US")
        a2 = self._make_order("AAPL.US")
        n = self._make_order("NVDA.US")
        svc = TradeNoteService(self._db())
        svc.upsert_note(a1, TradeNoteUpsert(note="a1"))
        svc.upsert_note(a2, TradeNoteUpsert(note="a2"))
        svc.upsert_note(n, TradeNoteUpsert(note="n"))
        assert svc.list_notes().total == 3
        assert svc.list_notes(symbol="AAPL.US").total == 2
        page1 = svc.list_notes(symbol="AAPL.US", page=1, page_size=1)
        assert len(page1.items) == 1
        assert page1.page == 1 and page1.page_size == 1

    def test_delete_is_idempotent(self) -> None:
        oid = self._make_order()
        svc = TradeNoteService(self._db())
        svc.upsert_note(oid, TradeNoteUpsert(note="x"))
        assert svc.delete_note(oid) is True
        assert svc.delete_note(oid) is False

    def test_tags_normalize(self) -> None:
        oid = self._make_order()
        svc = TradeNoteService(self._db())
        out = svc.upsert_note(oid, TradeNoteUpsert(
            note="x", tags=["  Good ", "Good", "", "Bad"],
        ))
        # trimmed, de-duplicated (exact match), empties dropped
        assert out.tags == ["Good", "Bad"]

    def test_analytics_aggregates(self) -> None:
        o1 = self._make_order("AAPL.US")
        o2 = self._make_order("AAPL.US")
        o3 = self._make_order("NVDA.US")
        svc = TradeNoteService(self._db())
        svc.upsert_note(o1, TradeNoteUpsert(note="a", tags=["good", "momentum"], rating=5))
        svc.upsert_note(o2, TradeNoteUpsert(note="b", tags=["good"], rating=3))
        svc.upsert_note(o3, TradeNoteUpsert(note="c", tags=[], rating=None))
        a = svc.analytics()
        assert a.total == 3
        assert a.rated_count == 2
        assert a.avg_rating == 4.0
        assert a.rating_distribution == {1: 0, 2: 0, 3: 1, 4: 0, 5: 1}
        assert a.distinct_symbols == 2
        assert a.top_tags[0].tag == "good"
        assert a.top_tags[0].count == 2


class TestTradeNoteAPI(_Base):
    def test_put_get_update_flow(self) -> None:
        oid = self._make_order("MSFT.US")
        resp = self.client.put(f"/api/trade-notes/{oid}", json={"note": "hello", "tags": ["a"], "rating": 5})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["note"] == "hello"
        assert body["tags"] == ["a"]
        assert body["rating"] == 5
        assert body["symbol"] == "MSFT.US"

        got = self.client.get(f"/api/trade-notes/{oid}")
        assert got.status_code == 200
        assert got.json()["note"] == "hello"

        upd = self.client.put(f"/api/trade-notes/{oid}", json={"note": "bye", "tags": [], "rating": None})
        assert upd.status_code == 200
        assert upd.json()["note"] == "bye"
        assert upd.json()["rating"] is None

    def test_put_404_when_order_missing(self) -> None:
        resp = self.client.put("/api/trade-notes/999999", json={"note": "x"})
        assert resp.status_code == 404

    def test_get_404_when_note_missing(self) -> None:
        oid = self._make_order()
        resp = self.client.get(f"/api/trade-notes/{oid}")
        assert resp.status_code == 404

    def test_list_endpoint(self) -> None:
        a = self._make_order("AAPL.US")
        n = self._make_order("NVDA.US")
        self.client.put(f"/api/trade-notes/{a}", json={"note": "a"})
        self.client.put(f"/api/trade-notes/{n}", json={"note": "n"})
        all_resp = self.client.get("/api/trade-notes")
        assert all_resp.status_code == 200
        assert all_resp.json()["total"] == 2
        filt = self.client.get("/api/trade-notes", params={"symbol": "AAPL.US"})
        assert filt.json()["total"] == 1

    def test_delete_endpoint_idempotent(self) -> None:
        oid = self._make_order()
        self.client.put(f"/api/trade-notes/{oid}", json={"note": "x"})
        d1 = self.client.delete(f"/api/trade-notes/{oid}")
        assert d1.status_code == 204
        # Gone from GET.
        assert self.client.get(f"/api/trade-notes/{oid}").status_code == 404
        # Idempotent.
        d2 = self.client.delete(f"/api/trade-notes/{oid}")
        assert d2.status_code == 204

    def test_rating_and_note_validation(self) -> None:
        oid = self._make_order()
        assert self.client.put(f"/api/trade-notes/{oid}", json={"rating": 0}).status_code == 422
        assert self.client.put(f"/api/trade-notes/{oid}", json={"rating": 6}).status_code == 422
        assert self.client.put(f"/api/trade-notes/{oid}", json={"note": "x" * 8001}).status_code == 422
