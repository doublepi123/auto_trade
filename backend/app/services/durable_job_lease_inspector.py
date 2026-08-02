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

from app.models import DurableJobLease

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

        Uses SQLite's clock for both observation time and status
        classification. Never mutates any row or session state.
        """
        # Get SQLite observation time in a single read snapshot.
        observed_at_epoch_ms = int(
            self._db.execute(text(f"SELECT {_DB_NOW_EPOCH_MS}")).scalar() or 0
        )

        rows = (
            self._db.query(DurableJobLease)
            .order_by(DurableJobLease.lease_key.asc())
            .all()
        )

        items: list[dict[str, Any]] = []
        active = 0
        reclaimable = 0
        expired = 0
        for row in rows:
            status = self._classify(row.expires_at_epoch_ms, observed_at_epoch_ms)
            if status == "ACTIVE":
                active += 1
            elif status == "RECLAIMABLE":
                reclaimable += 1
            else:
                expired += 1
            items.append(
                {
                    "lease_key": row.lease_key,
                    "fencing_token": row.fencing_token,
                    "acquired_at_epoch_ms": row.acquired_at_epoch_ms,
                    "renewed_at_epoch_ms": row.renewed_at_epoch_ms,
                    "expires_at_epoch_ms": row.expires_at_epoch_ms,
                    "status": status,
                    "holder_fingerprint": _holder_fingerprint(row.holder_id),
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
