from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app import database
from app.models import LLMInteraction, OrderRecord, RuntimeStateSnapshot, TradeEvent
from app.services.review_service import ReviewService


@pytest.fixture(autouse=True)
def fresh_db():
    # Truncate relevant tables instead of drop_all to avoid destroying schema
    # shared with other test modules.
    with database.SessionLocal() as db:
        db.query(TradeEvent).delete()
        db.query(OrderRecord).delete()
        db.query(LLMInteraction).delete()
        db.query(RuntimeStateSnapshot).delete()
        db.commit()
    yield


@pytest.fixture
def db_session():
    with database.SessionLocal() as db:
        yield db


def _make_llm(db, symbol="AAPL.US", created_at=None, applied=False, order_id=None):
    if created_at is None:
        created_at = datetime(2026, 5, 20, 10, 0, tzinfo=timezone.utc)
    interaction = LLMInteraction(
        symbol=symbol,
        market="US",
        success=True,
        order_action="BUY",
        order_id=order_id,
        applied=applied,
        created_at=created_at,
    )
    db.add(interaction)
    db.commit()
    return interaction


def _make_order(db, symbol="AAPL.US", side="BUY", price=100.0, executed_price=100.0, status="FILLED", created_at=None):
    if created_at is None:
        created_at = datetime(2026, 5, 20, 10, 1, tzinfo=timezone.utc)
    order = OrderRecord(
        broker_order_id=f"o{created_at.timestamp()}",
        symbol=symbol,
        side=side,
        quantity=10,
        price=price,
        executed_quantity=10,
        executed_price=executed_price,
        status=status,
        created_at=created_at,
    )
    db.add(order)
    db.commit()
    return order


def _make_event(db, event_type="ORDER_SKIPPED", symbol="AAPL.US", payload_json='{"skip_category":"FEE"}', created_at=None):
    if created_at is None:
        created_at = datetime(2026, 5, 20, 10, 2, tzinfo=timezone.utc)
    event = TradeEvent(
        event_type=event_type,
        symbol=symbol,
        payload_json=payload_json,
        created_at=created_at,
    )
    db.add(event)
    db.commit()
    return event


def _make_snapshot(db, symbol="AAPL.US", daily_pnl=0.0, last_price=100.0, created_at=None):
    if created_at is None:
        created_at = datetime(2026, 5, 20, 10, 3, tzinfo=timezone.utc)
    snapshot = RuntimeStateSnapshot(
        symbol=symbol,
        daily_pnl=daily_pnl,
        last_price=last_price,
        created_at=created_at,
    )
    db.add(snapshot)
    db.commit()
    return snapshot


def test_empty_review(db_session):
    svc = ReviewService(db_session)
    result = svc.get_review("AAPL.US", "2026-05-20", "2026-05-20")
    assert result["symbol"] == "AAPL.US"
    assert result["days"] == []
    assert result["total_pnl"] == 0.0


def test_review_with_single_day(db_session):
    _make_order(db_session, price=100.0, executed_price=100.0)
    _make_snapshot(db_session, daily_pnl=50.0, last_price=105.0)
    svc = ReviewService(db_session)
    result = svc.get_review("AAPL.US", "2026-05-20", "2026-05-20")
    assert len(result["days"]) == 1
    day = result["days"][0]
    assert day["date"] == "2026-05-20"
    assert day["daily_pnl"] == 50.0
    assert day["trade_count"] == 1


def test_fee_skip_tag(db_session):
    _make_event(db_session, event_type="ORDER_SKIPPED", payload_json='{"skip_category":"FEE"}')
    svc = ReviewService(db_session)
    result = svc.get_review("AAPL.US", "2026-05-20", "2026-05-20")
    assert result["days"][0]["error_tags"] == ["收益不足"]


def test_frequent_cancel_tag(db_session):
    for i in range(3):
        _make_event(db_session, event_type="ORDER_CANCELLED", payload_json="{}", created_at=datetime(2026, 5, 20, 10, i, tzinfo=timezone.utc))
    svc = ReviewService(db_session)
    result = svc.get_review("AAPL.US", "2026-05-20", "2026-05-20")
    assert "频繁重挂" in result["days"][0]["error_tags"]

def test_review_export_json(db_session):
    _make_order(db_session)
    _make_snapshot(db_session, daily_pnl=50.0)
    svc = ReviewService(db_session)
    buf = svc.export_review("AAPL.US", "2026-05-20", "2026-05-20", "json")
    import json
    data = json.loads(buf.read().decode("utf-8"))
    assert data["review"]["symbol"] == "AAPL.US"
    assert data["runtime_history"]["points"][0]["symbol"] == "AAPL.US"


def test_review_export_csv(db_session):
    _make_order(db_session)
    _make_snapshot(db_session, daily_pnl=50.0)
    svc = ReviewService(db_session)
    buf = svc.export_review("AAPL.US", "2026-05-20", "2026-05-20", "csv")
    content = buf.read().decode("utf-8")
    assert "section,row_type,date,symbol" in content
    assert "review_day,summary,2026-05-20,AAPL.US" in content
    assert "2026-05-20" in content


def test_review_export_json_includes_runtime_history_and_diagnostics(db_session):
    _make_order(db_session)
    _make_snapshot(db_session, symbol="AAPL.US", daily_pnl=50.0, last_price=105.0)
    _make_snapshot(db_session, symbol="NVDA.US", daily_pnl=12.0, last_price=221.0)
    svc = ReviewService(db_session)
    buf = svc.export_review(
        "AAPL.US",
        "2026-05-20",
        "2026-05-20",
        "json",
        diagnostics={
            "runner_running": True,
            "pending_order_symbols": ["AAPL.US"],
            "symbol_runtimes": [{"symbol": "AAPL.US", "engine_state": "long"}],
        },
    )
    import json

    data = json.loads(buf.read().decode("utf-8"))
    assert data["review"]["symbol"] == "AAPL.US"
    assert [point["symbol"] for point in data["runtime_history"]["points"]] == ["AAPL.US"]
    assert data["diagnostics"]["pending_order_symbols"] == ["AAPL.US"]
    assert data["diagnostics"]["symbol_runtimes"] == [{"symbol": "AAPL.US", "engine_state": "long"}]


def test_review_export_csv_contains_history_and_diagnostics_sections(db_session):
    _make_order(db_session)
    _make_snapshot(db_session, symbol="AAPL.US", daily_pnl=50.0, last_price=105.0)
    svc = ReviewService(db_session)
    buf = svc.export_review(
        "AAPL.US",
        "2026-05-20",
        "2026-05-20",
        "csv",
        diagnostics={
            "runner_running": True,
            "pending_order_symbols": ["AAPL.US"],
            "symbol_runtimes": [{"symbol": "AAPL.US", "engine_state": "long"}],
        },
    )
    content = buf.read().decode("utf-8")
    assert "section,row_type,date,symbol" in content
    assert "review_day,summary,2026-05-20,AAPL.US" in content
    assert "history_point,runtime_point,2026-05-20T10:03:00,AAPL.US" in content
    assert "diagnostic_runtime,runtime,AAPL.US,AAPL.US" in content
    assert "diagnostic_meta,pending_order_symbols,,AAPL.US" in content
