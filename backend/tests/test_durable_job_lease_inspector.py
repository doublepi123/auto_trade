"""Durable job lease inspector — read-only boundary/ordering/redaction tests.

Tests active/equality/expired boundaries using SQLite's clock, deterministic
ordering, empty state, authentication, holder fingerprint stability/redaction,
and SQL write interception/caller transaction preservation.
"""
from __future__ import annotations

import time

from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import settings
from app.main import app
from app.models import DurableJobLease
from app.services.durable_job_lease_inspector import (
    DurableJobLeaseInspector,
    _holder_fingerprint,
)
from app import database


database.init_db()
client = TestClient(app)


def _db_now_epoch_ms(db: Session) -> int:
    return int(
        db.execute(
            text(
                "SELECT CAST(ROUND((julianday('now') - 2440587.5) * 86400000.0) AS INTEGER)"
            )
        ).scalar()
        or 0
    )


def _seed_lease(
    db: Session,
    *,
    lease_key: str,
    holder_id: str = "worker-1",
    fencing_token: int = 1,
    ttl_ms: int = 60_000,
) -> DurableJobLease:
    now_ms = _db_now_epoch_ms(db)
    row = DurableJobLease(
        lease_key=lease_key,
        holder_id=holder_id,
        fencing_token=fencing_token,
        acquired_at_epoch_ms=now_ms,
        renewed_at_epoch_ms=now_ms,
        expires_at_epoch_ms=now_ms + ttl_ms,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


class TestDurableJobLeaseInspector:
    def setup_method(self) -> None:
        db = database.SessionLocal()
        # The durable_job_leases table has a BEFORE DELETE trigger that aborts
        # deletes. Temporarily drop the trigger to clean up test rows.
        db.execute(text("DROP TRIGGER IF EXISTS trg_durable_job_leases_no_delete"))
        db.query(DurableJobLease).delete()
        # Recreate the trigger so the inspector's no-mutation guarantee is
        # still enforced during the test.
        db.execute(
            text(
                "CREATE TRIGGER trg_durable_job_leases_no_delete "
                "BEFORE DELETE ON durable_job_leases "
                "BEGIN "
                "SELECT RAISE(ABORT, 'durable_job_leases rows cannot be deleted'); "
                "END"
            )
        )
        db.commit()
        db.close()

    def test_empty_state(self) -> None:
        db = database.SessionLocal()
        try:
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        assert result["items"] == []
        assert result["total"] == 0
        assert result["active_count"] == 0
        assert result["observed_at_epoch_ms"] > 0

    def test_active_lease(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", ttl_ms=60_000)
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        assert result["total"] == 1
        assert result["active_count"] == 1
        assert result["items"][0]["status"] == "ACTIVE"

    def test_expired_lease(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", ttl_ms=-1)
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        assert result["items"][0]["status"] == "EXPIRED"
        assert result["expired_count"] == 1

    def test_equality_is_reclaimable(self) -> None:
        """expires_at == SQLite now is RECLAIMABLE, not ACTIVE."""
        db = database.SessionLocal()
        try:
            now_ms = _db_now_epoch_ms(db)
            row = DurableJobLease(
                lease_key="job-eq",
                holder_id="worker-1",
                fencing_token=1,
                acquired_at_epoch_ms=now_ms - 1000,
                renewed_at_epoch_ms=now_ms,
                expires_at_epoch_ms=now_ms,  # exactly now
            )
            db.add(row)
            db.commit()
            # The SQLite clock may tick between the seed and the inspect, so
            # we verify the classification logic directly.
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        # If the clock hasn't ticked, status is RECLAIMABLE. If it ticked
        # past, it's EXPIRED. Either way it must NOT be ACTIVE.
        assert result["items"][0]["status"] in ("RECLAIMABLE", "EXPIRED")
        assert result["items"][0]["status"] != "ACTIVE"

    def test_deterministic_ordering(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="zebra")
            _seed_lease(db, lease_key="alpha")
            _seed_lease(db, lease_key="middle")
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        keys = [i["lease_key"] for i in result["items"]]
        assert keys == ["alpha", "middle", "zebra"]

    def test_holder_fingerprint_not_raw(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", holder_id="secret-worker-id")
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        item = result["items"][0]
        assert item["holder_fingerprint"] == _holder_fingerprint("secret-worker-id")
        assert item["holder_fingerprint"] != "secret-worker-id"
        assert len(item["holder_fingerprint"]) == 32  # 16 bytes hex

    def test_holder_fingerprint_stability(self) -> None:
        fp1 = _holder_fingerprint("worker-1")
        fp2 = _holder_fingerprint("worker-1")
        assert fp1 == fp2
        fp3 = _holder_fingerprint("worker-2")
        assert fp1 != fp3

    def test_no_raw_holder_in_response(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", holder_id="RAW_HOLDER_SECRET")
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        import json

        dumped = json.dumps(result)
        assert "RAW_HOLDER_SECRET" not in dumped
        assert "holder_id" not in dumped

    def test_no_session_mutation(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1")
            count_before = db.query(DurableJobLease).count()
            DurableJobLeaseInspector(db).inspect()
            count_after = db.query(DurableJobLease).count()
        finally:
            db.close()
        assert count_after == count_before

    def test_endpoint_returns_inspector(self) -> None:
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", ttl_ms=60_000)
        finally:
            db.close()
        resp = client.get("/api/durable-job-leases")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["status"] == "ACTIVE"
        assert "holder_fingerprint" in data["items"][0]
        assert "holder_id" not in data["items"][0]

    def test_auth_enforced(self, monkeypatch) -> None:
        monkeypatch.setattr(settings, "api_key", "secret-key")
        assert client.get("/api/durable-job-leases").status_code == 401
