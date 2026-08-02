"""Paginated LLM interaction browsing — service + API.

Tests pagination, filters (symbol, success, half-open datetime range), stable
newest-first tie ordering, safe projection (no prompt/raw/parsed/context),
authentication, invalid ranges, and legacy endpoint compatibility.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import Base, LLMInteraction
from app import database


database.init_db()
client = TestClient(app)


def _seed(
    db: Session,
    *,
    symbol: str = "AAPL.US",
    success: bool = True,
    prompt: str = "SECRET_PROMPT_CONTENT",
    raw_response: str = "SECRET_RAW_RESPONSE",
    parsed_response: str = '{"secret": "parsed"}',
    context_snapshot: str = '{"secret": "context"}',
    interaction_type: str = "analyze",
    order_action: str = "NONE",
    applied: bool = False,
    created_at: datetime | None = None,
) -> int:
    row = LLMInteraction(
        interaction_type=interaction_type,
        symbol=symbol,
        market="US",
        prompt=prompt,
        raw_response=raw_response,
        parsed_response=parsed_response,
        context_snapshot=context_snapshot,
        success=success,
        error="",
        order_action=order_action,
        applied=applied,
        created_at=created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


class TestLLMInteractionList:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        db.query(LLMInteraction).delete()
        db.commit()
        db.close()

    def test_empty(self) -> None:
        resp = client.get("/api/llm-interactions")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1
        assert data["page_size"] == 50

    def test_pagination(self) -> None:
        db = database.SessionLocal()
        try:
            for i in range(5):
                _seed(db, created_at=datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc))
        finally:
            db.close()
        resp = client.get("/api/llm-interactions", params={"page": 1, "page_size": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        resp2 = client.get("/api/llm-interactions", params={"page": 3, "page_size": 2})
        assert len(resp2.json()["items"]) == 1

    def test_symbol_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed(db, symbol="AAPL.US")
            _seed(db, symbol="MSFT.US")
        finally:
            db.close()
        resp = client.get("/api/llm-interactions", params={"symbol": "MSFT.US"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["symbol"] == "MSFT.US"

    def test_success_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed(db, success=True)
            _seed(db, success=False)
        finally:
            db.close()
        resp = client.get("/api/llm-interactions", params={"success": "true"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["success"] is True

    def test_datetime_range_half_open(self) -> None:
        db = database.SessionLocal()
        try:
            _seed(db, created_at=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc))
            _seed(db, created_at=datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc))
        finally:
            db.close()
        resp = client.get(
            "/api/llm-interactions",
            params={
                "from": "2026-06-14T10:00:00Z",
                "to": "2026-06-14T12:00:00Z",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        # Half-open [from, to): includes 10:00 but excludes 12:00
        assert data["total"] == 1

    def test_invalid_range_422(self) -> None:
        resp = client.get(
            "/api/llm-interactions",
            params={
                "from": "2026-06-14T12:00:00Z",
                "to": "2026-06-14T10:00:00Z",
            },
        )
        assert resp.status_code == 422

    def test_newest_first_tie_order(self) -> None:
        """Same created_at -> tie broken by id DESC."""
        db = database.SessionLocal()
        try:
            ts = datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc)
            id1 = _seed(db, created_at=ts)
            id2 = _seed(db, created_at=ts)
        finally:
            db.close()
        resp = client.get("/api/llm-interactions", params={"page_size": 10})
        assert resp.status_code == 200
        ids = [i["id"] for i in resp.json()["items"]]
        assert ids == sorted([id1, id2], reverse=True)

    def test_safe_projection_no_secrets(self) -> None:
        """Prompt, raw_response, parsed_response, context_snapshot must NOT escape."""
        db = database.SessionLocal()
        try:
            _seed(
                db,
                prompt="SECRET_PROMPT_CONTENT",
                raw_response="SECRET_RAW_RESPONSE",
                parsed_response='{"secret": "parsed"}',
                context_snapshot='{"secret": "context"}',
            )
        finally:
            db.close()
        resp = client.get("/api/llm-interactions")
        assert resp.status_code == 200
        data = resp.json()
        dumped = json.dumps(data)
        assert "SECRET_PROMPT_CONTENT" not in dumped
        assert "SECRET_RAW_RESPONSE" not in dumped
        assert '"secret"' not in dumped
        item = data["items"][0]
        # The safe list projection must not have these fields
        assert "prompt" not in item
        assert "raw_response" not in item
        assert "parsed_response" not in item
        assert "context_snapshot" not in item

    def test_detail_route_not_broken(self) -> None:
        """The /{id} route must still work alongside the list route."""
        db = database.SessionLocal()
        try:
            row_id = _seed(db)
        finally:
            db.close()
        resp = client.get(f"/api/llm-interactions/{row_id}")
        assert resp.status_code == 200
        assert "prompt" in resp.json()

    def test_auth_enforced(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "api_key", "secret-key")
        assert client.get("/api/llm-interactions").status_code == 401
        resp = client.get(
            "/api/llm-interactions",
            headers={"X-API-Key": "secret-key"},
        )
        assert resp.status_code == 200

    def test_page_size_capped(self) -> None:
        db = database.SessionLocal()
        try:
            for i in range(3):
                _seed(db, created_at=datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc))
        finally:
            db.close()
        # The Query param caps page_size at 200; requesting 200 returns all 3.
        resp = client.get("/api/llm-interactions", params={"page_size": 200})
        assert resp.status_code == 200
        assert resp.json()["page_size"] == 200
        assert len(resp.json()["items"]) == 3
