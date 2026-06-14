from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone

os.environ.setdefault("AUTO_TRADE_DATABASE_URL", f"sqlite:///{tempfile.gettempdir()}/test_timeline_search.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.database import SessionLocal, init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.models import AuditLog, TradeEvent  # noqa: E402


def _seed() -> None:
    init_db()
    with SessionLocal() as db:
        db.query(TradeEvent).delete()
        db.query(AuditLog).delete()
        db.add_all([
            TradeEvent(
                event_type="ORDER_FILLED",
                symbol="AAPL.US",
                status="FILLED",
                message="Filled buy 100 @ 100.50",
                payload_json=json.dumps({"qty": 100}),
                created_at=datetime.now(timezone.utc),
            ),
            TradeEvent(
                event_type="ORDER_SKIPPED",
                symbol="TSLA.US",
                status="SKIPPED",
                message="Skipped: fee guard",
                payload_json=json.dumps({"skip_category": "FEE"}),
                created_at=datetime.now(timezone.utc),
            ),
            TradeEvent(
                event_type="LLM_ANALYSIS",
                symbol="MSFT.US",
                status="SUCCESS",
                message="LLM suggested buy_low 99 / sell_high 110",
                payload_json="{}",
                created_at=datetime.now(timezone.utc),
            ),
            AuditLog(
                action="STRATEGY_UPDATE",
                severity="INFO",
                actor_hash="deadbeef",
                request_summary=json.dumps({"changed": {"buy_low": 99.0}}),
                result="SUCCESS",
                created_at=datetime.now(timezone.utc),
            ),
            AuditLog(
                action="KILL_SWITCH",
                severity="CRITICAL",
                actor_hash="cafe0000",
                request_summary="kill switch engaged",
                result="SUCCESS",
                created_at=datetime.now(timezone.utc),
            ),
        ])
        db.commit()


def test_q_search_filters_message_substring() -> None:
    _seed()
    client = TestClient(app)
    resp = client.get("/api/events", params={"q": "fee guard", "source": "trade", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    types = [item["event_type"] for item in body["items"]]
    assert types == ["ORDER_SKIPPED"]


def test_q_search_matches_symbol_case_insensitive() -> None:
    _seed()
    client = TestClient(app)
    resp = client.get("/api/events", params={"q": "tsla", "source": "trade", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    symbols = [item["symbol"] for item in body["items"]]
    assert "TSLA.US" in symbols
    assert "AAPL.US" not in symbols


def test_q_search_audit_action() -> None:
    _seed()
    client = TestClient(app)
    resp = client.get("/api/events", params={"q": "kill", "source": "audit", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    actions = [item["event_type"] for item in body["items"]]
    assert "KILL_SWITCH" in actions
    assert "STRATEGY_UPDATE" not in actions


def test_q_search_all_source_merges_both() -> None:
    _seed()
    client = TestClient(app)
    resp = client.get("/api/events", params={"q": "buy_low", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    # Both an LLM_ANALYSIS trade row and the STRATEGY_UPDATE audit row should match
    sources = {item["source"] for item in body["items"]}
    assert "trade" in sources
    assert "audit" in sources


def test_q_search_empty_returns_all() -> None:
    _seed()
    client = TestClient(app)
    resp = client.get("/api/events", params={"q": "", "page_size": 50})
    assert resp.status_code == 200
    body = resp.json()
    # All 5 seeded rows should be present
    assert body["total"] >= 5
