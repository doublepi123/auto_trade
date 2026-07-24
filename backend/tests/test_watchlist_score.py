from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from typing import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

os.environ["AUTO_TRADE_DATABASE_URL"] = f"sqlite:///{tempfile.gettempdir()}/test_watchlist_score.db"
os.environ["DEEPSEEK_API_KEY"] = ""

from app.main import app  # noqa: E402
from app.models import Base, WatchlistItem, WatchlistScore  # noqa: E402
from app.services.watchlist_score_service import (  # noqa: E402
    DEFAULT_ACTION,
    DEFAULT_SCORE,
    WatchlistScoreService,
    _parse_llm_payload,
)
from app.database import SessionLocal, engine, init_db  # noqa: E402


@pytest.fixture
def db_session() -> Iterator:
    init_db()
    db = SessionLocal()
    try:
        # Clean slate
        db.query(WatchlistScore).delete()
        db.query(WatchlistItem).delete()
        db.commit()
        yield db
    finally:
        db.close()


@pytest.fixture
def client() -> Iterator:
    with TestClient(app) as c:
        yield c


class TestParseLlmPayload:
    def test_parses_clean_json(self) -> None:
        raw = json.dumps({
            "score": 72.5,
            "confidence": 0.8,
            "recommended_action": "BUY",
            "rationale": "Strong momentum",
        })
        out = _parse_llm_payload(raw)
        assert out["score"] == 72.5
        assert out["confidence"] == 0.8
        assert out["recommended_action"] == "BUY"
        assert out["rationale"] == "Strong momentum"

    def test_parses_markdown_fenced(self) -> None:
        raw = "```json\n" + json.dumps({"score": 55, "confidence": 0.5, "recommended_action": "HOLD", "rationale": "meh"}) + "\n```"
        out = _parse_llm_payload(raw)
        assert out["score"] == 55
        assert out["recommended_action"] == "HOLD"

    def test_tolerates_prose_around_json(self) -> None:
        raw = 'The result is {"score": 33, "confidence": 0.4, "recommended_action": "AVOID", "rationale": "weak"}.'
        out = _parse_llm_payload(raw)
        assert out["score"] == 33
        assert out["recommended_action"] == "AVOID"

    def test_missing_fields_yield_empty(self) -> None:
        out = _parse_llm_payload("definitely not json")
        assert out == {}


class TestWatchlistScoreService:
    def test_record_score_clamps_to_range(self, db_session) -> None:
        svc = WatchlistScoreService(db_session)
        too_high = svc.record_score(symbol="AAPL.US", market="US", score=150.0)
        too_low = svc.record_score(symbol="AAPL.US", market="US", score=-25.0)
        assert too_high.score == 100.0
        assert too_low.score == 0.0

    def test_record_score_truncates_rationale(self, db_session) -> None:
        svc = WatchlistScoreService(db_session)
        huge = "x" * 5000
        row = svc.record_score(symbol="AAPL.US", market="US", score=50.0, rationale=huge)
        assert len(row.rationale) == 4000

    def test_list_latest_per_symbol_returns_one_row(self, db_session) -> None:
        svc = WatchlistScoreService(db_session)
        svc.record_score(symbol="AAPL.US", market="US", score=10.0)
        # Backdate the previous row
        old = svc.get_latest("AAPL.US")
        assert old is not None
        old.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
        old.expires_at = datetime.now(timezone.utc) - timedelta(minutes=30)
        db_session.commit()
        svc.record_score(symbol="AAPL.US", market="US", score=80.0)
        svc.record_score(symbol="MSFT.US", market="US", score=42.0)
        latest = svc.list_latest_per_symbol()
        symbols = {row.symbol for row in latest}
        assert symbols == {"AAPL.US", "MSFT.US"}
        aapl_latest = next(row for row in latest if row.symbol == "AAPL.US")
        assert aapl_latest.score == 80.0

    def test_quant_score_remains_primary_after_ai_review(
        self,
        db_session,
    ) -> None:
        svc = WatchlistScoreService(db_session)
        quant = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=56,
            recommended_action="CANDIDATE",
            source="quant_v1",
        )
        review = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=88,
            recommended_action="BUY",
            source="llm",
        )

        primary = svc.list_latest_per_symbol()
        by_family = svc.list_latest_per_symbol_and_family()

        assert primary == [quant]
        assert {row.id for row in by_family} == {quant.id, review.id}

    def test_freshness_check(self, db_session) -> None:
        svc = WatchlistScoreService(db_session)
        fresh = svc.record_score(symbol="AAPL.US", market="US", score=50.0, ttl_minutes=10)
        assert svc.is_fresh(fresh)
        # Manually backdate
        fresh.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()
        assert not svc.is_fresh(fresh)

    def test_prune_history_removes_only_expired_retention_window(
        self,
        db_session,
    ) -> None:
        svc = WatchlistScoreService(db_session)
        old = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=40,
        )
        old.created_at = datetime.now(timezone.utc) - timedelta(days=31)
        db_session.commit()
        recent = svc.record_score(
            symbol="MSFT.US",
            market="US",
            score=60,
        )
        old_id = old.id
        recent_id = recent.id

        deleted = svc.prune_history(retention_days=30)

        assert deleted == 1
        assert db_session.get(WatchlistScore, old_id) is None
        assert db_session.get(WatchlistScore, recent_id) is not None

    def test_fallback_when_llm_unconfigured(self, db_session, monkeypatch) -> None:
        """With no DEEPSEEK_API_KEY the service must return a deterministic
        fallback rather than raising — the UI must keep working."""
        from types import SimpleNamespace

        monkeypatch.setattr(
            "app.config.settings",
            SimpleNamespace(deepseek_api_key=""),
        )
        svc = WatchlistScoreService(db_session)
        row = svc.score_from_llm_or_fallback(symbol="AAPL.US", market="US", ttl_minutes=5)
        assert row.source.startswith("fallback_")
        assert row.score == DEFAULT_SCORE
        assert row.recommended_action == DEFAULT_ACTION
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        assert expires > datetime.now(timezone.utc)

    def test_configured_llm_uses_provider_response_content(
        self, db_session, monkeypatch
    ) -> None:
        from types import SimpleNamespace

        from app.services.llm_advisor_service import (
            LLMAdvisorService,
            LLMProviderResponse,
            LLMTokenUsage,
        )

        monkeypatch.setattr(
            "app.config.settings",
            SimpleNamespace(deepseek_api_key="configured"),
        )

        def fake_call(
            _advisor: LLMAdvisorService, _prompt: str
        ) -> LLMProviderResponse:
            return LLMProviderResponse(
                content=json.dumps(
                    {
                        "score": 78,
                        "confidence": 0.9,
                        "recommended_action": "BUY",
                        "rationale": "Investor's setup",
                    }
                ),
                usage=LLMTokenUsage(prompt_tokens=12, completion_tokens=8, total_tokens=20),
            )

        monkeypatch.setattr(LLMAdvisorService, "_call_deepseek", fake_call)

        row = WatchlistScoreService(db_session).score_from_llm_or_fallback(
            symbol="AAPL.US", market="US", ttl_minutes=5
        )

        assert row.source == "llm"
        assert row.score == 78.0
        assert row.confidence == 0.9
        assert row.recommended_action == "BUY"
        assert row.rationale == "Investor's setup"

    def test_minimax_provider_does_not_require_deepseek_key(
        self,
        db_session,
        monkeypatch,
    ) -> None:
        from types import SimpleNamespace

        from app.services import llm_advisor_service
        from app.services.llm_advisor_service import (
            LLMAdvisorService,
            LLMProviderResponse,
            LLMTokenUsage,
        )

        configured = SimpleNamespace(
            llm_provider="minimax",
            minimax_api_key="configured",
            deepseek_api_key="",
        )
        monkeypatch.setattr("app.config.settings", configured)
        monkeypatch.setattr(llm_advisor_service, "settings", configured)

        def fake_call(
            _advisor: LLMAdvisorService,
            _prompt: str,
        ) -> LLMProviderResponse:
            return LLMProviderResponse(
                content=json.dumps(
                    {
                        "score": 82,
                        "confidence": 0.88,
                        "recommended_action": "BUY",
                        "rationale": "MiniMax provider result",
                    }
                ),
                usage=LLMTokenUsage(
                    prompt_tokens=10,
                    completion_tokens=6,
                    total_tokens=16,
                ),
            )

        monkeypatch.setattr(LLMAdvisorService, "_call_minimax", fake_call)

        row = WatchlistScoreService(
            db_session
        ).score_from_llm_or_fallback(
            symbol="AAPL.US",
            market="US",
            ttl_minutes=5,
        )

        assert row.source == "llm"
        assert row.score == 82
        assert row.rationale == "MiniMax provider result"


class TestWatchlistScoreAPI:
    def test_post_score_endpoint_returns_fallback(self, client: TestClient, monkeypatch) -> None:
        from types import SimpleNamespace

        monkeypatch.setattr(
            "app.config.settings",
            SimpleNamespace(deepseek_api_key=""),
        )
        resp = client.post(
            "/api/watchlist/score",
            json={"symbol": "AAPL.US", "market": "US", "ttl_minutes": 5},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["symbol"] == "AAPL.US"
        assert data["market"] == "US"
        assert data["source"].startswith("fallback_")
        assert data["score"] == DEFAULT_SCORE

    def test_get_scores_keeps_reviews_out_of_quant_results(
        self,
        client: TestClient,
    ) -> None:
        # Seed two reviews for the same symbol + one for another.
        client.post("/api/watchlist/score", json={"symbol": "AAPL.US", "market": "US"})
        client.post("/api/watchlist/score", json={"symbol": "AAPL.US", "market": "US"})
        client.post("/api/watchlist/score", json={"symbol": "MSFT.US", "market": "US"})

        resp = client.get("/api/watchlist/scores")
        assert resp.status_code == 200
        body = resp.json()
        assert body["scores"] == []
        reviews = body["reviews"]
        symbols = {row["symbol"] for row in reviews}
        assert {"AAPL.US", "MSFT.US"}.issubset(symbols)
        assert sum(1 for row in reviews if row["symbol"] == "AAPL.US") == 1

    def test_get_scores_exposes_only_current_quant_generation(
        self,
        client: TestClient,
        db_session,
    ) -> None:
        now = datetime.now(timezone.utc)
        svc = WatchlistScoreService(db_session)
        current = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=61,
            recommended_action="CANDIDATE",
            source="quant_v5",
        )
        current.created_at = now - timedelta(minutes=10)
        current.expires_at = now + timedelta(minutes=30)
        legacy_newer = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=99,
            recommended_action="CANDIDATE",
            source="quant_v4",
        )
        legacy_newer.created_at = now - timedelta(minutes=1)
        svc.record_score(
            symbol="MSFT.US",
            market="US",
            score=0,
            recommended_action="AVOID",
            source="quant_error_v5",
        )
        svc.record_score(
            symbol="AMD.US",
            market="US",
            score=88,
            recommended_action="CANDIDATE",
            source="quant_v1",
        )
        svc.record_score(
            symbol="NVDA.US",
            market="US",
            score=0,
            recommended_action="AVOID",
            source="quant_error",
        )
        db_session.commit()

        response = client.get("/api/watchlist/scores")

        assert response.status_code == 200
        scores = {
            row["symbol"]: row
            for row in response.json()["scores"]
        }
        assert set(scores) == {"AAPL.US", "MSFT.US"}
        assert scores["AAPL.US"]["source"] == "quant_v5"
        assert scores["AAPL.US"]["score"] == 61
        assert scores["MSFT.US"]["source"] == "quant_error_v5"
        assert scores["MSFT.US"]["recommended_action"] == "AVOID"

    def test_scored_snapshots_ignore_newer_legacy_quant_generation(
        self,
        client: TestClient,
        db_session,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from types import SimpleNamespace

        from app.core.broker import Quote

        db_session.add(
            WatchlistItem(
                symbol="AAPL.US",
                market="US",
                alias="Apple",
            )
        )
        svc = WatchlistScoreService(db_session)
        current = svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=61,
            recommended_action="CANDIDATE",
            source="quant_v5",
        )
        current.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        svc.record_score(
            symbol="AAPL.US",
            market="US",
            score=99,
            recommended_action="CANDIDATE",
            source="quant_v4",
        )
        db_session.commit()
        broker = SimpleNamespace(
            get_quotes=lambda _symbols: [
                Quote(
                    "AAPL.US",
                    210.0,
                    209.99,
                    210.01,
                    datetime.now(timezone.utc).isoformat(),
                )
            ]
        )
        monkeypatch.setattr(
            "app.api.watchlist.get_runner",
            lambda: SimpleNamespace(broker=broker),
        )

        response = client.get("/api/watchlist/scored-snapshots")

        assert response.status_code == 200
        assert response.json()[0]["score"] == 61

    def test_post_score_rejects_bad_market(self, client: TestClient) -> None:
        resp = client.post(
            "/api/watchlist/score",
            json={"symbol": "AAPL.US", "market": "JP"},
        )
        assert resp.status_code == 422
