"""Ops Unified Timeline — llm + risk sources merged into /api/events. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_ops_timeline_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import AuditLog, Base, LLMInteraction, RiskEvent, TradeEvent
from app.services.event_list_service import list_timeline_events


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
        db = Session(bind=self.engine)
        for model in (TradeEvent, AuditLog, LLMInteraction, RiskEvent):
            db.query(model).delete()
        db.commit()
        db.close()

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _seed(self) -> None:
        db = self._db()
        db.add(TradeEvent(event_type="ORDER_FILLED", symbol="AAPL.US", side="BUY", status="SUCCESS", message="filled"))
        db.add(AuditLog(action="CONTROL_START", severity="INFO", request_summary="started"))
        db.add(LLMInteraction(interaction_type="analyze", symbol="AAPL.US", success=True, order_action="BUY", applied=True))
        db.add(LLMInteraction(interaction_type="analyze", symbol="NVDA.US", success=False, error="rate limited"))
        db.add(RiskEvent(event_type="DAILY_LOSS", reason="daily loss limit reached"))
        db.commit()
        db.close()


class TestOpsTimelineService(_Base):
    def test_llm_source(self) -> None:
        self._seed()
        items, total = list_timeline_events(self._db(), source="llm", event_types=None, symbol=None, page=1, page_size=50)
        assert total == 2
        assert all(i.source == "llm" for i in items)

    def test_risk_source(self) -> None:
        self._seed()
        items, total = list_timeline_events(self._db(), source="risk", event_types=None, symbol=None, page=1, page_size=50)
        assert total == 1
        assert items[0].source == "risk"
        assert items[0].event_type == "DAILY_LOSS"

    def test_all_merges_four_sources(self) -> None:
        self._seed()
        items, total = list_timeline_events(self._db(), source="all", event_types=None, symbol=None, page=1, page_size=50)
        sources = {i.source for i in items}
        assert sources == {"trade", "audit", "llm", "risk"}
        assert total == 5

    def test_llm_symbol_filter(self) -> None:
        self._seed()
        items, total = list_timeline_events(self._db(), source="llm", event_types=None, symbol="AAPL.US", page=1, page_size=50)
        assert total == 1
        assert items[0].symbol == "AAPL.US"

    def test_llm_failed_carries_warning(self) -> None:
        self._seed()
        items, _ = list_timeline_events(self._db(), source="llm", event_types=None, symbol="NVDA.US", page=1, page_size=50)
        assert items[0].severity == "WARNING"
        assert items[0].status == "FAILED"


class TestOpsTimelineAPI(_Base):
    def test_llm_endpoint(self) -> None:
        self._seed()
        resp = self.client.get("/api/events", params={"source": "llm"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["total"] == 2

    def test_all_endpoint_has_four_sources(self) -> None:
        self._seed()
        resp = self.client.get("/api/events", params={"source": "all", "page_size": 50})
        assert resp.status_code == 200
        sources = {i["source"] for i in resp.json()["items"]}
        assert sources == {"trade", "audit", "llm", "risk"}

    def test_invalid_source_422(self) -> None:
        resp = self.client.get("/api/events", params={"source": "bogus"})
        assert resp.status_code == 422
