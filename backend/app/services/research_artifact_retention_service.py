"""Bounded retention for reproducible research artifact payloads.

Strategy v2 forward replay bytes are immutable, content-addressed, and
recomputable offline from provider data plus the frozen code manifest. They
are therefore legitimate to expire on a window. The provenance rows that
prove what was computed — ``strategy_v2_forward_evidence`` /
``strategy_v2_forward_registrations`` with their SHA-256 commitments — are
never touched by these prunes. Historical quant-v6 tables are no longer
written or pruned; their rows, if present, stay in place.

Follows the established maintenance idiom: batched short transactions with an
optional durable-lease ``transaction_fence`` and ``operation_checkpoint``, and
``retention_days=0`` disables pruning. SQLite reuses freed pages; file-size
reclaim requires the offline VACUUM in ``scripts/database_maintenance.py``.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app.models import (
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardReplayArtifact,
)


@dataclass(frozen=True)
class ForwardReplayArtifactPruneResult:
    bindings_deleted: int = 0
    artifacts_deleted: int = 0
    batches: int = 0


class ResearchArtifactRetentionService:
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

    @staticmethod
    def _validate_window(retention_days: int, batch_size: int) -> bool:
        if retention_days < 0:
            raise ValueError("retention_days must be non-negative")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        return retention_days > 0

    def prune_expired_forward_replay_artifacts(
        self,
        *,
        retention_days: int,
        batch_size: int,
        max_batches: int | None = 8,
        now: datetime | None = None,
    ) -> ForwardReplayArtifactPruneResult:
        """Expire replay bytes for forward evidence older than the window.

        The evidence row keeps its input/result SHA-256 commitments, so
        expiring the recomputable replay bundle does not destroy proof. A
        replay artifact referenced by any surviving binding (e.g. shared with
        newer evidence) is retained.
        """
        self._checkpoint_operation()
        if not self._validate_window(retention_days, batch_size):
            self._checkpoint_operation()
            return ForwardReplayArtifactPruneResult()
        if max_batches is not None and max_batches <= 0:
            self._checkpoint_operation()
            return ForwardReplayArtifactPruneResult()

        cutoff = (now or datetime.now(timezone.utc)) - timedelta(
            days=retention_days
        )
        bindings_deleted = 0
        artifacts_deleted = 0
        batches = 0

        while max_batches is None or batches < max_batches:
            self._checkpoint_operation()
            evidence_ids = [
                int(row[0])
                for row in (
                    self._db.query(StrategyV2ForwardEvidence.id)
                    .filter(
                        StrategyV2ForwardEvidence.evaluated_at < cutoff,
                        self._db.query(StrategyV2ForwardEvidenceArtifact)
                        .filter(
                            StrategyV2ForwardEvidenceArtifact.evidence_id
                            == StrategyV2ForwardEvidence.id
                        )
                        .exists(),
                    )
                    .order_by(
                        StrategyV2ForwardEvidence.evaluated_at.asc(),
                        StrategyV2ForwardEvidence.id.asc(),
                    )
                    .limit(batch_size)
                    .all()
                )
            ]
            if not evidence_ids:
                break
            try:
                self._fence_in_transaction()
                bindings_deleted += int(
                    self._db.query(StrategyV2ForwardEvidenceArtifact)
                    .filter(
                        StrategyV2ForwardEvidenceArtifact.evidence_id.in_(
                            evidence_ids
                        )
                    )
                    .delete(synchronize_session=False)
                )
                artifacts_deleted += (
                    self._delete_orphaned_forward_replay_artifacts(
                        cutoff=cutoff,
                        batch_size=batch_size,
                    )
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            batches += 1

        while max_batches is None or batches < max_batches:
            self._checkpoint_operation()
            try:
                self._fence_in_transaction()
                orphans = self._delete_orphaned_forward_replay_artifacts(
                    cutoff=cutoff,
                    batch_size=batch_size,
                )
                self._db.commit()
            except Exception:
                self._db.rollback()
                raise
            if orphans == 0:
                break
            artifacts_deleted += orphans
            batches += 1
        self._checkpoint_operation()
        return ForwardReplayArtifactPruneResult(
            bindings_deleted=bindings_deleted,
            artifacts_deleted=artifacts_deleted,
            batches=batches,
        )

    def _delete_orphaned_forward_replay_artifacts(
        self,
        *,
        cutoff: datetime,
        batch_size: int,
    ) -> int:
        referenced = self._db.query(
            StrategyV2ForwardEvidenceArtifact.artifact_sha256
        )
        digests = [
            str(row[0])
            for row in (
                self._db.query(StrategyV2ForwardReplayArtifact.digest_sha256)
                .filter(
                    StrategyV2ForwardReplayArtifact.created_at < cutoff,
                    StrategyV2ForwardReplayArtifact.digest_sha256.not_in(
                        referenced
                    ),
                )
                .order_by(
                    StrategyV2ForwardReplayArtifact.created_at.asc(),
                    StrategyV2ForwardReplayArtifact.digest_sha256.asc(),
                )
                .limit(batch_size)
                .all()
            )
        ]
        if not digests:
            return 0
        return int(
            self._db.query(StrategyV2ForwardReplayArtifact)
            .filter(
                StrategyV2ForwardReplayArtifact.digest_sha256.in_(digests),
                StrategyV2ForwardReplayArtifact.created_at < cutoff,
                StrategyV2ForwardReplayArtifact.digest_sha256.not_in(
                    referenced
                ),
            )
            .delete(synchronize_session=False)
        )
