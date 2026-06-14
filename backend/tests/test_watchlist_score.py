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

    def test_freshness_check(self, db_session) -> None:
        svc = WatchlistScoreService(db_session)
        fresh = svc.record_score(symbol="AAPL.US", market="US", score=50.0, ttl_minutes=10)
        assert svc.is_fresh(fresh)
        # Manually backdate
        fresh.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db_session.commit()
        assert not svc.is_fresh(fresh)

    def test_fallback_when_llm_unconfigured(self, db_session) -> None:
        """With no DEEPSEEK_API_KEY the service must return a deterministic
        fallback rather than raising — the UI must keep working."""
        svc = WatchlistScoreService(db_session)
        row = svc.score_from_llm_or_fallback(symbol="AAPL.US", market="US", ttl_minutes=5)
        assert row.source.startswith("fallback_")
        assert row.score == DEFAULT_SCORE
        assert row.recommended_action == DEFAULT_ACTION
        expires = row.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        assert expires > datetime.now(timezone.utc)


class TestWatchlistScoreAPI:
    def test_post_score_endpoint_returns_fallback(self, client: TestClient) -> None:
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

    def test_get_scores_lists_latest_per_symbol(self, client: TestClient) -> None:
        # Seed two scores for the same symbol + one for another
        client.post("/api/watchlist/score", json={"symbol": "AAPL.US", "market": "US"})
        client.post("/api/watchlist/score", json={"symbol": "AAPL.US", "market": "US"})
        client.post("/api/watchlist/score", json={"symbol": "MSFT.US", "market": "US"})

        resp = client.get("/api/watchlist/scores")
        assert resp.status_code == 200
        body = resp.json()
        scores = body["scores"]
        symbols = {row["symbol"] for row in scores}
        assert {"AAPL.US", "MSFT.US"}.issubset(symbols)
        # AAPL appears exactly once (latest per symbol)
        assert sum(1 for row in scores if row["symbol"] == "AAPL.US") == 1

    def test_post_score_rejects_bad_market(self, client: TestClient) -> None:
        resp = client.post(
            "/api/watchlist/score",
            json={"symbol": "AAPL.US", "market": "JP"},
        )
        assert resp.status_code == 422
