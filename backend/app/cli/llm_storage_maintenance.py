from __future__ import annotations

import argparse
import json
import sys

from app.config import settings
from app.database import SessionLocal, engine
from app.services.llm_interaction_service import LLMInteractionService


def _vacuum_sqlite() -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        checkpoint = connection.exec_driver_sql(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).one()
        try:
            busy = int(checkpoint[0])
            log_frames = int(checkpoint[1])
            checkpointed_frames = int(checkpoint[2])
        except (IndexError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "SQLite WAL checkpoint returned an unexpected result"
            ) from exc
        if busy != 0:
            raise RuntimeError(
                "SQLite WAL checkpoint is busy "
                f"(busy={busy}, log_frames={log_frames}, "
                f"checkpointed_frames={checkpointed_frames}); VACUUM was not run"
            )
        connection.exec_driver_sql("VACUUM")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prune and compact LLM interaction history. VACUUM is available only "
            "with an explicit confirmation that the backend service is stopped."
        )
    )
    parser.add_argument(
        "--retention-days",
        type=int,
        default=settings.llm_interaction_retention_days,
    )
    parser.add_argument(
        "--no-action-retention-days",
        type=int,
        default=settings.llm_no_action_retention_days,
    )
    parser.add_argument(
        "--context-max-bytes",
        type=int,
        default=settings.llm_context_snapshot_max_bytes,
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=settings.llm_storage_maintenance_batch_size,
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help="Rebuild the SQLite file after maintenance (requires stopped backend).",
    )
    parser.add_argument(
        "--confirm-service-stopped",
        action="store_true",
        help="Confirm that no backend process is using the database.",
    )
    return parser


def main() -> int:
    parser = _parser()
    args = parser.parse_args()
    if args.retention_days < 0 or args.no_action_retention_days < 0:
        parser.error("retention windows must be non-negative")
    if args.context_max_bytes < 2048:
        parser.error("--context-max-bytes must be at least 2048")
    if args.batch_size <= 0:
        parser.error("--batch-size must be positive")
    if args.vacuum and not args.confirm_service_stopped:
        parser.error("--vacuum requires --confirm-service-stopped")
    if args.vacuum and engine.dialect.name != "sqlite":
        parser.error("--vacuum is supported only for SQLite")

    db = SessionLocal()
    try:
        service = LLMInteractionService(db)
        pruned = service.prune_expired(
            retention_days=args.retention_days,
            no_action_retention_days=args.no_action_retention_days,
            batch_size=args.batch_size,
            max_batches=None,
        )
        compacted = service.compact_oversized_contexts(
            max_bytes=args.context_max_bytes,
            batch_size=min(25, args.batch_size),
            max_rows=None,
        )
    finally:
        db.close()

    vacuumed = False
    if args.vacuum:
        try:
            _vacuum_sqlite()
        except RuntimeError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        vacuumed = True

    print(
        json.dumps(
            {
                "deleted": pruned.deleted,
                "delete_batches": pruned.batches,
                "compacted": compacted.compacted,
                "compaction_batches": compacted.batches,
                "vacuumed": vacuumed,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
