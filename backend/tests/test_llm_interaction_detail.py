"""LLM interaction detail — service + API. Per-file sqlite."""
from __future__ import annotations

import os
import tempfile

os.environ["AUTO_TRADE_DATABASE_URL"] = (
    f"sqlite:///{tempfile.gettempdir()}/auto_trade_test_llm_detail_{os.getpid()}.db"
)

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import get_db
from app.main import app
from app.models import Base
from app.services.llm_interaction_service import LLMInteractionService


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

    def _db(self) -> Session:
        return Session(bind=self.engine)

    def _make(self) -> int:
        svc = LLMInteractionService(self._db())
        record = svc.create(
            interaction_type="analyze", symbol="AAPL.US", market="US",
            prompt="suggest interval", raw_response='{"buy_low": 90, "sell_high": 190}',
            parsed_response={"buy_low": 90, "sell_high": 190},
            context_snapshot={"price": 120, "position": "flat"},
            success=True, error="", order_action="BUY",
            prompt_tokens=100, completion_tokens=25, total_tokens=125,
        )
        return record.id


class TestLLMInteractionDetailService(_Base):
    def test_get_detail_parses_json(self) -> None:
        rid = self._make()
        out = LLMInteractionService(self._db()).get_detail(rid)
        assert out is not None
        assert out.prompt == "suggest interval"
        assert out.parsed_response == {"buy_low": 90, "sell_high": 190}
        assert out.context_snapshot["price"] == 120
        assert out.order_action == "BUY"
        assert out.prompt_tokens == 100
        assert out.completion_tokens == 25
        assert out.total_tokens == 125

    def test_get_detail_missing(self) -> None:
        assert LLMInteractionService(self._db()).get_detail(999999) is None

    def test_update_outcome_merges_policy_audit_into_parsed_response(self) -> None:
        rid = self._make()
        policy_outcome = {
            "code": "PRICE_DEVIATION",
            "reference_price": 100.0,
            "candidate_price": 102.0,
            "deviation_pct": 2.0,
            "confidence": 0.84,
            "disposition": "REJECT",
        }

        LLMInteractionService(self._db()).update_outcome(
            rid,
            applied=False,
            order_status="POLICY_REJECTED",
            policy_outcome=policy_outcome,
        )

        out = LLMInteractionService(self._db()).get_detail(rid)
        assert out is not None
        assert out.parsed_response["buy_low"] == 90
        assert out.parsed_response["policy_outcome"] == policy_outcome
        assert out.order_status == "POLICY_REJECTED"


class TestLLMInteractionDetailAPI(_Base):
    def test_endpoint(self) -> None:
        rid = self._make()
        resp = self.client.get(f"/api/llm-interactions/{rid}")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["prompt"] == "suggest interval"
        assert data["parsed_response"]["buy_low"] == 90
        assert data["prompt_tokens"] == 100
        assert data["completion_tokens"] == 25
        assert data["total_tokens"] == 125

    def test_endpoint_404(self) -> None:
        resp = self.client.get("/api/llm-interactions/999999")
        assert resp.status_code == 404

    def test_list_endpoint_includes_token_usage(self) -> None:
        self._make()

        resp = self.client.get("/api/strategy/llm-interval/interactions?limit=1")

        assert resp.status_code == 200, resp.text
        assert resp.json()[0]["prompt_tokens"] == 100
        assert resp.json()[0]["completion_tokens"] == 25
        assert resp.json()[0]["total_tokens"] == 125
