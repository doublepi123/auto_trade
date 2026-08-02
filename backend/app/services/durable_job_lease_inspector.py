"""Durable job lease inspector — read-only, authenticated.

Classifies existing ``DurableJobLease`` rows using SQLite's clock (the same
clock the acquisition path uses). ``expires_at > SQLite now`` is ACTIVE;
equality is RECLAIMABLE, matching acquisition semantics. Never exposes raw
``holder_id``; never acquires, heartbeats, releases, deletes, updates, flushes,
or commits leases.
"""
from __future__ import annotations

import hashlib
from typing import Any, Literal

from sqlalchemy import text
from sqlalchemy.orm import Session

__all__ = ["DurableJobLeaseInspector"]

# Same SQLite-clock expression as the acquisition path.
_DB_NOW_EPOCH_MS = (
    "CAST(ROUND((julianday('now') - 2440587.5) * 86400000.0) AS INTEGER)"
)

_LeaseStatus = Literal["ACTIVE", "RECLAIMABLE", "EXPIRED"]


class DurableJobLeaseInspector:
    """Read-only inspector over ``DurableJobLease`` rows."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def inspect(self) -> dict[str, Any]:
        """Return observation time, lease rows, and counts.

        Observation time and all lease rows come from ONE SQLite snapshot via
        a single SQL statement with a clock CTE. This prevents a concurrent
        heartbeat/takeover from splitting the evidence. Never mutates any row
        or session state.
        """
        # Single SQL statement: the clock CTE fixes the SQLite observation time
        # for the entire query, so every row is classified against the same
        # clock value. We SELECT the clock as a standalone row (via UNION ALL)
        # so the observation time is always returned even when the table is
        # empty. This is one atomic SQLite read.
        sql = text(
            f"""
            WITH observation AS (SELECT {_DB_NOW_EPOCH_MS} AS now_ms)
            SELECT
                d.lease_key,
                d.fencing_token,
                d.acquired_at_epoch_ms,
                d.renewed_at_epoch_ms,
                d.expires_at_epoch_ms,
                d.holder_id,
                o.now_ms
            FROM durable_job_leases d
            CROSS JOIN observation o
            UNION ALL
            SELECT NULL, NULL, NULL, NULL, NULL, NULL, now_ms FROM observation
            ORDER BY lease_key ASC
            """
        )
        raw_rows = self._db.execute(sql).all()

        items: list[dict[str, Any]] = []
        active = 0
        reclaimable = 0
        expired = 0
        observed_at_epoch_ms = 0
        for raw in raw_rows:
            # The last row is the clock-only row (lease_key IS NULL); it
            # carries the observation time even when the table is empty.
            if raw.lease_key is None:
                observed_at_epoch_ms = int(raw.now_ms)
                continue
            observed_at_epoch_ms = int(raw.now_ms)
            expires_at = int(raw.expires_at_epoch_ms)
            status = self._classify(expires_at, observed_at_epoch_ms)
            if status == "ACTIVE":
                active += 1
            elif status == "RECLAIMABLE":
                reclaimable += 1
            else:
                expired += 1
            items.append(
                {
                    "lease_key": raw.lease_key,
                    "fencing_token": int(raw.fencing_token),
                    "acquired_at_epoch_ms": int(raw.acquired_at_epoch_ms),
                    "renewed_at_epoch_ms": int(raw.renewed_at_epoch_ms),
                    "expires_at_epoch_ms": expires_at,
                    "status": status,
                    "holder_fingerprint": _holder_fingerprint(raw.holder_id),
                }
            )

        return {
            "observed_at_epoch_ms": observed_at_epoch_ms,
            "items": items,
            "total": len(items),
            "active_count": active,
            "reclaimable_count": reclaimable,
            "expired_count": expired,
        }

    @staticmethod
    def _classify(expires_at_epoch_ms: int, observed_at_epoch_ms: int) -> _LeaseStatus:
        """Classify: expires_at > now is ACTIVE; equality is RECLAIMABLE."""
        if expires_at_epoch_ms > observed_at_epoch_ms:
            return "ACTIVE"
        if expires_at_epoch_ms == observed_at_epoch_ms:
            return "RECLAIMABLE"
        return "EXPIRED"


def _holder_fingerprint(holder_id: str) -> str:
    """Stable SHA-256 fingerprint of the raw holder_id (first 16 hex chars).

    Never exposes the raw ``holder_id``.
    """
    digest = hashlib.sha256(holder_id.encode("utf-8")).digest()
    return digest[:16].hex()
