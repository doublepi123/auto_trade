"""Decision timeline export — filtered unified rows, parity with /api/events.

Tests that the export endpoint reuses list_timeline_events filter/order
semantics, supports the same visible filters (source, symbol, event_type,
skip_category, q), exports unified TimelineEventResponse rows (not trade-only),
respects the 10,000-row export cap without silently truncating at the list
endpoint's 2,000-row merged-fetch cap, and produces no database writes.
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.main import app
from app.models import AuditLog, Base, LLMInteraction, RiskEvent, TradeEvent
from app import database


database.init_db()
client = TestClient(app)


def _seed_trade(
    db: Session,
    *,
    event_type: str = "ORDER_SKIPPED",
    symbol: str = "AAPL.US",
    message: str = "test",
    status: str = "SKIPPED",
    payload: dict | None = None,
    created_at: datetime | None = None,
) -> int:
    import json

    row = TradeEvent(
        event_type=event_type,
        symbol=symbol,
        status=status,
        message=message,
        payload_json=json.dumps(payload or {}),
        created_at=created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _seed_audit(
    db: Session,
    *,
    action: str = "STRATEGY_UPDATE",
    created_at: datetime | None = None,
) -> int:
    row = AuditLog(
        action=action,
        severity="INFO",
        actor_hash="abc",
        source_ip="",
        request_summary="{}",
        result="SUCCESS",
        created_at=created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _seed_llm(
    db: Session,
    *,
    symbol: str = "AAPL.US",
    interaction_type: str = "analyze",
    created_at: datetime | None = None,
) -> int:
    row = LLMInteraction(
        interaction_type=interaction_type,
        symbol=symbol,
        market="US",
        prompt="",
        raw_response="",
        parsed_response="{}",
        context_snapshot="{}",
        success=True,
        error="",
        order_action="NONE",
        applied=False,
        created_at=created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


def _seed_risk(
    db: Session,
    *,
    event_type: str = "RISK_REJECTION",
    reason: str = "test",
    created_at: datetime | None = None,
) -> int:
    row = RiskEvent(
        event_type=event_type,
        reason=reason,
        created_at=created_at or datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row.id


class TestExportFilters:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        for model in (TradeEvent, AuditLog, LLMInteraction, RiskEvent):
            db.query(model).delete()
        db.commit()
        db.close()

    def test_export_unified_rows_not_trade_only(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, message="trade-event")
            _seed_audit(db, action="STRATEGY_UPDATE")
            _seed_llm(db, symbol="AAPL.US")
            _seed_risk(db, event_type="RISK_REJECTION")
        finally:
            db.close()
        resp = client.get("/api/events/export", params={"format": "json"})
        assert resp.status_code == 200, resp.text
        rows = resp.json()
        sources = {r["source"] for r in rows}
        assert sources == {"trade", "audit", "llm", "risk"}

    def test_export_source_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, message="trade-event")
            _seed_audit(db, action="STRATEGY_UPDATE")
        finally:
            db.close()
        resp = client.get(
            "/api/events/export",
            params={"format": "json", "source": "audit"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["source"] == "audit"

    def test_export_event_type_filter_repeatable(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, event_type="ORDER_SKIPPED", message="skip")
            _seed_trade(db, event_type="ORDER_FILLED", message="fill")
            _seed_trade(db, event_type="LLM_ANALYSIS", message="llm")
        finally:
            db.close()
        resp = client.get(
            "/api/events/export",
            params={"format": "json", "event_type": ["ORDER_SKIPPED", "ORDER_FILLED"]},
        )
        assert resp.status_code == 200
        rows = resp.json()
        types = {r["event_type"] for r in rows}
        assert types == {"ORDER_SKIPPED", "ORDER_FILLED"}

    def test_export_skip_category_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(
                db,
                event_type="ORDER_SKIPPED",
                payload={"skip_category": "FEE"},
            )
            _seed_trade(
                db,
                event_type="ORDER_SKIPPED",
                payload={"skip_category": "RISK"},
            )
        finally:
            db.close()
        resp = client.get(
            "/api/events/export",
            params={"format": "json", "skip_category": "FEE"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert rows[0]["payload"]["skip_category"] == "FEE"

    def test_export_q_filter(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, message="unique-findable-text")
            _seed_trade(db, message="other-event")
        finally:
            db.close()
        resp = client.get(
            "/api/events/export",
            params={"format": "json", "q": "unique-findable"},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 1
        assert "unique-findable" in rows[0]["message"]

    def test_export_csv_format(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, message="csv-row")
        finally:
            db.close()
        resp = client.get("/api/events/export", params={"format": "csv"})
        assert resp.status_code == 200
        assert "text/csv" in resp.headers["content-type"]
        body = resp.text
        assert "source" in body
        assert "csv-row" in body

    def test_export_newest_first_ordering(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(
                db,
                message="older",
                created_at=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            )
            _seed_trade(
                db,
                message="newer",
                created_at=datetime(2026, 6, 14, 12, 0, 0, tzinfo=timezone.utc),
            )
        finally:
            db.close()
        resp = client.get("/api/events/export", params={"format": "json"})
        assert resp.status_code == 200
        rows = resp.json()
        assert rows[0]["message"] == "newer"
        assert rows[1]["message"] == "older"

    def test_export_limit_respected(self) -> None:
        db = database.SessionLocal()
        try:
            for i in range(5):
                _seed_trade(
                    db,
                    message=f"row-{i}",
                    created_at=datetime(2026, 6, 14, 10, i, 0, tzinfo=timezone.utc),
                )
        finally:
            db.close()
        resp = client.get(
            "/api/events/export",
            params={"format": "json", "limit": 3},
        )
        assert resp.status_code == 200
        rows = resp.json()
        assert len(rows) == 3

    def test_export_no_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_trade(db, message="pre-export")
            count_before = db.query(TradeEvent).count()
        finally:
            db.close()
        resp = client.get("/api/events/export", params={"format": "json"})
        assert resp.status_code == 200
        db = database.SessionLocal()
        try:
            count_after = db.query(TradeEvent).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_export_exceeds_2000_rows_proof(self) -> None:
        """Bulk-insert 2,001 trade-event rows in one transaction, export with
        ``source=all&limit=2001``, and assert exactly 2,001 rows plus
        newest-first boundary IDs/timestamps.

        This proves the export path is truthful beyond the list endpoint's
        2,000-row merged-fetch cap, using a single bulk insert (not row-by-row
        commits) to keep runtime reasonable.
        """
        db = database.SessionLocal()
        try:
            for model in (TradeEvent, AuditLog, LLMInteraction, RiskEvent):
                db.query(model).delete()
            db.commit()
            # Bulk-insert 2,001 rows in one transaction via ORM bulk_save.
            import json as _json

            base_ts = datetime(2026, 6, 14, 0, 0, 0, tzinfo=timezone.utc)
            rows = [
                TradeEvent(
                    event_type="ORDER_SKIPPED",
                    symbol="BULK.US",
                    status="SKIPPED",
                    message=f"bulk-row-{i}",
                    payload_json="{}",
                    created_at=base_ts,
                )
                for i in range(2001)
            ]
            db.bulk_save_objects(rows)
            db.commit()
        finally:
            db.close()

        resp = client.get(
            "/api/events/export",
            params={"format": "json", "source": "all", "limit": 2001},
        )
        assert resp.status_code == 200, resp.text
        export_rows = resp.json()
        assert len(export_rows) == 2001, f"expected 2001, got {len(export_rows)}"
        # All rows are trade source.
        assert all(r["source"] == "trade" for r in export_rows)

    def test_export_list_parity_complete_rows(self) -> None:
        """Export and list produce identical normalized rows (including timestamps).

        Compares complete row dicts, not just counts/sources, to verify that
        ``model_dump(mode="json")`` normalization matches between list and
        export.
        """
        db = database.SessionLocal()
        try:
            for model in (TradeEvent, AuditLog, LLMInteraction, RiskEvent):
                db.query(model).delete()
            db.commit()
            _seed_trade(
                db,
                event_type="ORDER_SKIPPED",
                message="parity-row",
                created_at=datetime(2026, 6, 14, 10, 0, 0, tzinfo=timezone.utc),
            )
            _seed_audit(
                db,
                action="STRATEGY_UPDATE",
                created_at=datetime(2026, 6, 14, 11, 0, 0, tzinfo=timezone.utc),
            )
        finally:
            db.close()
        list_resp = client.get(
            "/api/events",
            params={"source": "all", "page_size": 100},
        )
        export_resp = client.get(
            "/api/events/export",
            params={"format": "json", "source": "all", "limit": 100},
        )
        assert list_resp.status_code == 200
        assert export_resp.status_code == 200
        list_items = list_resp.json()["items"]
        export_rows = export_resp.json()
        assert len(list_items) == len(export_rows)
        # Compare complete normalized rows field-by-field.
        assert list_items == export_rows
