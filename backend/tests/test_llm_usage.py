"""LLM token usage summary API."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.config import settings
from app.database import get_db
from app.main import app
from app.models import Base, LLMInteraction
from app.services.llm_usage_service import LLMUsageService


class TestLLMUsageSummary:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            with Session(bind=cls.engine) as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()

    def setup_method(self) -> None:
        with Session(bind=self.engine) as db:
            db.query(LLMInteraction).delete()
            db.commit()

    def test_seeded_rows_are_aggregated_by_day_and_type(self) -> None:
        today = datetime.now(timezone.utc) - timedelta(minutes=1)
        yesterday = today - timedelta(days=1)
        with Session(bind=self.engine) as db:
            db.add_all(
                [
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=100,
                        completion_tokens=40,
                        total_tokens=140,
                        created_at=yesterday,
                    ),
                    LLMInteraction(
                        interaction_type="preview",
                        success=False,
                        prompt_tokens=60,
                        completion_tokens=None,
                        total_tokens=60,
                        created_at=today,
                    ),
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=None,
                        completion_tokens=20,
                        total_tokens=20,
                        created_at=today,
                    ),
                    LLMInteraction(
                        interaction_type="analyze",
                        success=True,
                        prompt_tokens=999,
                        completion_tokens=999,
                        total_tokens=1998,
                        created_at=today - timedelta(days=40),
                    ),
                ]
            )
            db.commit()

        response = self.client.get("/api/llm-usage/summary?days=30")

        assert response.status_code == 200, response.text
        assert response.json() == {
            "days": 30,
            "total_interactions": 3,
            "successful_interactions": 2,
            "total_prompt_tokens": 160,
            "total_completion_tokens": 60,
            "total_tokens": 220,
            "by_day": [
                {
                    "date": yesterday.date().isoformat(),
                    "interactions": 1,
                    "prompt_tokens": 100,
                    "completion_tokens": 40,
                    "total_tokens": 140,
                },
                {
                    "date": today.date().isoformat(),
                    "interactions": 2,
                    "prompt_tokens": 60,
                    "completion_tokens": 20,
                    "total_tokens": 80,
                },
            ],
            "by_type": [
                {
                    "interaction_type": "analyze",
                    "interactions": 2,
                    "total_tokens": 160,
                },
                {
                    "interaction_type": "preview",
                    "interactions": 1,
                    "total_tokens": 60,
                },
            ],
        }

    def test_empty_database_returns_zero_totals(self) -> None:
        response = self.client.get("/api/llm-usage/summary?days=30")

        assert response.status_code == 200
        assert response.json() == {
            "days": 30,
            "total_interactions": 0,
            "successful_interactions": 0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_tokens": 0,
            "by_day": [],
            "by_type": [],
        }

    def test_days_must_be_between_one_and_365(self) -> None:
        assert self.client.get("/api/llm-usage/summary?days=0").status_code == 422
        assert self.client.get("/api/llm-usage/summary?days=366").status_code == 422


class TestLLMUsageBySymbol:
    @classmethod
    def setup_class(cls) -> None:
        cls.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(bind=cls.engine)

        def override_get_db():
            with Session(bind=cls.engine) as db:
                yield db

        app.dependency_overrides[get_db] = override_get_db
        cls.client = TestClient(app)

    @classmethod
    def teardown_class(cls) -> None:
        app.dependency_overrides.pop(get_db, None)
        cls.engine.dispose()

    def setup_method(self) -> None:
        settings.api_key = ""
        with Session(bind=self.engine) as db:
            db.query(LLMInteraction).delete()
            db.commit()

    def _add(self, **kw) -> None:
        with Session(bind=self.engine) as db:
            db.add(LLMInteraction(
                interaction_type=kw.get("interaction_type", "analyze"),
                symbol=kw.get("symbol", ""),
                market=kw.get("market", "US"),
                success=kw.get("success", True),
                prompt_tokens=kw.get("prompt_tokens", 100),
                completion_tokens=kw.get("completion_tokens", 40),
                total_tokens=kw.get("total_tokens", 140),
                created_at=kw.get("created_at", datetime.now(timezone.utc) - timedelta(minutes=1)),
            ))
            db.commit()

    def test_aggregation_by_symbol(self) -> None:
        self._add(symbol="AAPL.US", market="US", success=True, prompt_tokens=100, completion_tokens=40, total_tokens=140)
        self._add(symbol="AAPL.US", market="US", success=False, prompt_tokens=50, completion_tokens=10, total_tokens=60)
        self._add(symbol="TSLA.US", market="US", success=True, prompt_tokens=200, completion_tokens=100, total_tokens=300)
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["days"] == 30
        assert data["limit"] == 50
        assert data["total_groups"] == 2
        by_symbol = {i["symbol"]: i for i in data["items"]}
        aapl = by_symbol["AAPL.US"]
        assert aapl["market"] == "US"
        assert aapl["interactions"] == 2
        assert aapl["successful_interactions"] == 1
        assert aapl["success_rate"] == 0.5
        assert aapl["prompt_tokens"] == 150
        assert aapl["completion_tokens"] == 50
        assert aapl["total_tokens"] == 200
        assert aapl["latest_interaction_at"] is not None
        tsla = by_symbol["TSLA.US"]
        assert tsla["interactions"] == 1
        assert tsla["total_tokens"] == 300

    def test_ordering_total_tokens_desc_then_interactions_desc(self) -> None:
        # TSLA: 300 tokens, 1 interaction. AAPL: 200 tokens, 2 interactions.
        self._add(symbol="AAPL.US", total_tokens=100)
        self._add(symbol="AAPL.US", total_tokens=100)
        self._add(symbol="TSLA.US", total_tokens=300)
        # BOND: 200 tokens, 1 interaction — ties with AAPL on total tokens but
        # AAPL has more interactions, so AAPL sorts before BOND.
        self._add(symbol="BOND.US", total_tokens=200)
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        data = resp.json()
        symbols = [i["symbol"] for i in data["items"]]
        assert symbols == ["TSLA.US", "AAPL.US", "BOND.US"]

    def test_tiebreak_symbol_asc_when_tokens_and_interactions_equal(self) -> None:
        # Two symbols with identical totals+interactions: symbol asc decides.
        self._add(symbol="ZZZ.US", total_tokens=100)
        self._add(symbol="AAA.US", total_tokens=100)
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        data = resp.json()
        symbols = [i["symbol"] for i in data["items"]]
        assert symbols == ["AAA.US", "ZZZ.US"]

    def test_blank_symbol_represented_as_unspecified(self) -> None:
        self._add(symbol="", market="US", total_tokens=100)
        self._add(symbol="AAPL.US", market="US", total_tokens=200)
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        data = resp.json()
        symbols = [i["symbol"] for i in data["items"]]
        assert "UNSPECIFIED" in symbols
        assert "AAPL.US" in symbols
        unspec = next(i for i in data["items"] if i["symbol"] == "UNSPECIFIED")
        assert unspec["interactions"] == 1
        assert unspec["total_tokens"] == 100

    def test_days_filter_excludes_old_rows(self) -> None:
        self._add(symbol="AAPL.US", total_tokens=100, created_at=datetime.now(timezone.utc) - timedelta(days=40))
        self._add(symbol="TSLA.US", total_tokens=300, created_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        data = resp.json()
        assert data["total_groups"] == 1
        assert [i["symbol"] for i in data["items"]] == ["TSLA.US"]

    def test_limit_truncates_but_total_groups_reflects_full_count(self) -> None:
        for sym in ("A.US", "B.US", "C.US"):
            self._add(symbol=sym, total_tokens=100)
        resp = self.client.get("/api/llm-usage/by-symbol?days=30&limit=2")
        data = resp.json()
        assert data["limit"] == 2
        assert data["total_groups"] == 3
        assert len(data["items"]) == 2

    def test_empty_data(self) -> None:
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_groups"] == 0
        assert data["items"] == []

    def test_days_validation(self) -> None:
        assert self.client.get("/api/llm-usage/by-symbol?days=0").status_code == 422
        assert self.client.get("/api/llm-usage/by-symbol?days=366").status_code == 422

    def test_limit_validation(self) -> None:
        assert self.client.get("/api/llm-usage/by-symbol?limit=0").status_code == 422
        assert self.client.get("/api/llm-usage/by-symbol?limit=201").status_code == 422

    def test_no_sensitive_fields_exposed(self) -> None:
        self._add(symbol="AAPL.US", market="US")
        resp = self.client.get("/api/llm-usage/by-symbol?days=30")
        data = resp.json()
        item = data["items"][0]
        # Only the safe aggregate fields are present — no prompt text, raw/parsed
        # response, errors, order ids or context.
        allowed = {
            "symbol", "market", "interactions", "successful_interactions",
            "success_rate", "prompt_tokens", "completion_tokens",
            "total_tokens", "latest_interaction_at",
        }
        assert set(item.keys()) == allowed
        assert "prompt" not in {k for k in item if k != "prompt_tokens"}
        assert "raw_response" not in item
        assert "parsed_response" not in item
        assert "context_snapshot" not in item
        assert "order_id" not in item
        assert "error" not in item

    def test_auth_enforced_when_api_key_configured(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "llm-secret")
        assert self.client.get("/api/llm-usage/by-symbol").status_code == 401
        resp = self.client.get("/api/llm-usage/by-symbol", headers={"X-API-Key": "llm-secret"})
        assert resp.status_code == 200

    def test_service_direct_aggregation(self) -> None:
        self._add(symbol="AAPL.US", market="US", success=True, total_tokens=140)
        self._add(symbol="AAPL.US", market="US", success=False, total_tokens=60)
        with Session(bind=self.engine) as db:
            resp = LLMUsageService(db).by_symbol(days=30, limit=50)
        assert resp.total_groups == 1
        assert resp.items[0].symbol == "AAPL.US"
        assert resp.items[0].interactions == 2
        assert resp.items[0].successful_interactions == 1
        assert resp.items[0].success_rate == 0.5
        assert resp.items[0].total_tokens == 200
