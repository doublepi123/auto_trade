"""Daily risk history — reads runtime_state_snapshots to show risk over time."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RuntimeStateSnapshot
from app.schemas import RiskHistoryPoint, RiskHistoryResponse


@dataclass(frozen=True)
class RuntimeStateSnapshotPruneResult:
    deleted: int = 0
    batches: int = 0


class RiskHistoryService:
    def __init__(
        self,
        db: Session,
        *,
        transaction_fence: Callable[[Session], object] | None = None,
        operation_checkpoint: Callable[[], object] | None = None,
    ) -> None:
        self._db = db
        self._transaction_fence = transaction_fence
        self._operation_checkpoint = operation_checkpoint

    def _checkpoint_operation(self) -> None:
        if self._operation_checkpoint is not None:
            self._operation_checkpoint()

    def _fence_in_transaction(self) -> None:
        if self._transaction_fence is not None:
            self._transaction_fence(self._db)

    def prune_expired_snapshots(
        self,
        *,
        retention_days: int,
        batch_size: int,
        max_batches: int | None = 8,
        now: datetime | None = None,
    ) -> RuntimeStateSnapshotPruneResult:
        """Bound observational runtime-state history growth.

        The runner writes one row per poll for every tracked symbol, so this
        table dominates database size on long-running deployments. Snapshots are
        pure observability: risk decisions read live runner state, never these
        rows. The newest snapshot per symbol is always retained so the risk
        history panel keeps a `latest` value even after a long idle period.
        """
        self._checkpoint_operation()
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        if retention_days == 0 or (max_batches is not None and max_batches <= 0):
            self._checkpoint_operation()
            return RuntimeStateSnapshotPruneResult()

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=retention_days)
        retained_ids = self._latest_snapshot_ids()

        deleted = 0
        batches = 0
        cursor_id = 0
        while max_batches is None or batches < max_batches:
            self._checkpoint_operation()
            rows = (
                self._db.query(RuntimeStateSnapshot.id)
                .filter(
                    RuntimeStateSnapshot.created_at < cutoff,
                    RuntimeStateSnapshot.id > cursor_id,
                )
                .order_by(RuntimeStateSnapshot.id.asc())
                .limit(batch_size)
                .all()
            )
            if not rows:
                break
            scanned = [int(row[0]) for row in rows]
            cursor_id = scanned[-1]
            ids = [row_id for row_id in scanned if row_id not in retained_ids]
            if ids:
                self._fence_in_transaction()
                self._db.query(RuntimeStateSnapshot).filter(
                    RuntimeStateSnapshot.id.in_(ids)
                ).delete(synchronize_session=False)
                self._db.commit()
                deleted += len(ids)
                batches += 1
            if len(scanned) < batch_size:
                break
        self._checkpoint_operation()
        return RuntimeStateSnapshotPruneResult(deleted=deleted, batches=batches)

    def _latest_snapshot_ids(self) -> set[int]:
        rows = (
            self._db.query(RuntimeStateSnapshot.symbol)
            .distinct()
            .all()
        )
        latest: set[int] = set()
        for (symbol,) in rows:
            row = (
                self._db.query(RuntimeStateSnapshot.id)
                .filter(RuntimeStateSnapshot.symbol == symbol)
                .order_by(
                    RuntimeStateSnapshot.created_at.desc(),
                    RuntimeStateSnapshot.id.desc(),
                )
                .first()
            )
            if row is not None:
                latest.add(int(row[0]))
        return latest

    def get_history(
        self,
        *,
        symbol: str | None = None,
        limit: int = 100,
        from_dt: datetime | None = None,
        to_dt: datetime | None = None,
        max_limit: int = 500,
    ) -> RiskHistoryResponse:
        capped = max(1, min(limit, max_limit))
        stmt = select(RuntimeStateSnapshot)
        if symbol:
            stmt = stmt.where(RuntimeStateSnapshot.symbol == symbol)
        if from_dt is not None:
            stmt = stmt.where(RuntimeStateSnapshot.created_at >= from_dt)
        if to_dt is not None:
            stmt = stmt.where(RuntimeStateSnapshot.created_at < to_dt)
        stmt = stmt.order_by(RuntimeStateSnapshot.created_at.desc()).limit(capped)
        rows = list(self._db.scalars(stmt))
        rows = list(reversed(rows))  # chronological for charting
        points = [
            RiskHistoryPoint(
                created_at=r.created_at,
                engine_state=r.engine_state,
                paused=bool(r.paused),
                kill_switch=bool(r.kill_switch),
                daily_pnl=float(r.daily_pnl),
                consecutive_losses=int(r.consecutive_losses),
            )
            for r in rows
        ]
        return RiskHistoryResponse(points=points, latest=points[-1] if points else None)
