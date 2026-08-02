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
    _DB_NOW_EPOCH_MS,
    _holder_fingerprint,
)
from app import database


database.init_db()
client = TestClient(app)

# Canonical trigger DDL — centralized so setup and teardown stay in sync.
_DROP_TRIGGER_SQL = "DROP TRIGGER IF EXISTS trg_durable_job_leases_no_delete"
_CREATE_TRIGGER_SQL = (
    "CREATE TRIGGER trg_durable_job_leases_no_delete "
    "BEFORE DELETE ON durable_job_leases "
    "BEGIN "
    "SELECT RAISE(ABORT, 'durable_job_leases rows cannot be deleted'); "
    "END"
)


def _cleanup_lease_rows() -> None:
    """Drop the no-delete trigger, delete all test rows, recreate the trigger.

    The production ``durable_job_leases`` table has a BEFORE DELETE trigger
    that aborts all deletes. This helper temporarily drops it, removes test
    rows, and recreates the exact canonical trigger so the inspector's
    no-mutation guarantee is still enforced during tests.
    """
    db = database.SessionLocal()
    try:
        db.execute(text(_DROP_TRIGGER_SQL))
        db.query(DurableJobLease).delete()
        db.execute(text(_CREATE_TRIGGER_SQL))
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
        _cleanup_lease_rows()

    def teardown_method(self) -> None:
        """Symmetric cleanup after every test, including the final one.

        Without this, the last test's seeded row remains in the shared per-PID
        database and triggers an abort when ``test_e2e_restart.py``'s generic
        cleanup later issues ``DELETE FROM durable_job_leases``.
        """
        _cleanup_lease_rows()

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

    def test_equality_is_reclaimable_deterministic(self) -> None:
        """expires_at == observed clock is exactly RECLAIMABLE.

        Inserts a lease whose ``expires_at_epoch_ms`` is computed by the SAME
        SQLite clock expression the inspector uses, within one SQL statement.
        Because the inspector's clock CTE and this insert run within the same
        second (SQLite ``julianday('now')`` has ~1ms resolution), the observed
        clock in the inspect call will be >= the inserted expiry. When they
        match exactly, the status is RECLAIMABLE. When the clock ticks past,
        it is EXPIRED. Either way it must NOT be ACTIVE.

        To prove the equality boundary directly, we also test the classifier
        with a fixed clock value.
        """
        # Direct classifier test: equality is RECLAIMABLE.
        assert DurableJobLeaseInspector._classify(1000, 1000) == "RECLAIMABLE"
        assert DurableJobLeaseInspector._classify(1001, 1000) == "ACTIVE"
        assert DurableJobLeaseInspector._classify(999, 1000) == "EXPIRED"

        # Integrated test: insert with the SQLite clock, then inspect.
        db = database.SessionLocal()
        try:
            db.execute(
                text(
                    "INSERT INTO durable_job_leases "
                    "(lease_key, holder_id, fencing_token, "
                    " acquired_at_epoch_ms, renewed_at_epoch_ms, "
                    " expires_at_epoch_ms) "
                    "VALUES ('job-eq', 'worker-1', 1, "
                    f"  {_DB_NOW_EPOCH_MS} - 1000, {_DB_NOW_EPOCH_MS}, "
                    f"  {_DB_NOW_EPOCH_MS})"
                )
            )
            db.commit()
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        assert result["items"][0]["status"] in ("RECLAIMABLE", "EXPIRED")
        assert result["items"][0]["status"] != "ACTIVE"

    def test_one_snapshot_no_split(self) -> None:
        """Observation time and rows come from one SQL statement (clock CTE).

        The ``observed_at_epoch_ms`` must equal the clock value used to
        classify every row, proving a single snapshot.
        """
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", ttl_ms=60_000)
            _seed_lease(db, lease_key="job-2", ttl_ms=-1000)
            result = DurableJobLeaseInspector(db).inspect()
        finally:
            db.close()
        observed = result["observed_at_epoch_ms"]
        assert observed > 0
        # Every row's status is consistent with the single observed clock.
        for item in result["items"]:
            if item["expires_at_epoch_ms"] > observed:
                assert item["status"] == "ACTIVE"
            elif item["expires_at_epoch_ms"] == observed:
                assert item["status"] == "RECLAIMABLE"
            else:
                assert item["status"] == "EXPIRED"

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

    def test_no_sql_writes_interception(self, monkeypatch) -> None:
        """The inspector must never issue INSERT/UPDATE/DELETE.

        Intercept the session's execute to fail on any DML, proving the
        inspector is read-only at the SQL level.
        """
        db = database.SessionLocal()
        try:
            _seed_lease(db, lease_key="job-1", ttl_ms=60_000)
        finally:
            db.close()

        db = database.SessionLocal()
        try:
            original_execute = db.execute

            def _guard_execute(stmt, *args, **kwargs):
                sql_text = str(stmt)
                upper = sql_text.upper()
                for keyword in ("INSERT", "UPDATE", "DELETE", "REPLACE"):
                    if keyword in upper:
                        raise AssertionError(
                            f"inspector must not issue {keyword}: {sql_text[:80]}"
                        )
                return original_execute(stmt, *args, **kwargs)

            monkeypatch.setattr(db, "execute", _guard_execute)
            result = DurableJobLeaseInspector(db).inspect()
            assert result["total"] == 1
        finally:
            db.close()
