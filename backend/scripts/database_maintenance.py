#!/usr/bin/env python3
"""Read-only-by-default database maintenance: report, prune, vacuum, backups.

Default mode is PREVIEW: per-table page usage (dbstat), exact per-job counts
of what retention *would* delete, a projected post-retention size, and the
backup relocation plan — all without touching anything. Mutation requires
``--apply``. ``--vacuum`` additionally requires ``--apply``, refuses to run
while any configured market is inside regular trading hours (VACUUM rebuilds
the whole file and blocks writers; it also needs roughly the database size
in free disk space), and checkpoints the WAL first.

This command has no broker access, no order path, and never touches the live
evidence tables (orders, transactions, trade_events, audit_logs, risk_events,
tracked_entries, strategy_v2_shadow_trades) or the provenance rows
(quant-v6 publications/registrations, forward evidence/registrations).
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Sequence, TypedDict

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.config import settings
from app.core.market_calendar import is_trading_hours, supported_markets
from app.models import (
    StrategyV2ForwardEvidence,
    StrategyV2ForwardEvidenceArtifact,
    StrategyV2ForwardReplayArtifact,
    StrategyV2ShadowDecision,
    WatchlistQuantV6Artifact,
    WatchlistQuantV6Publication,
    WatchlistQuantV6PublicationArtifact,
)
from app.services.research_artifact_retention_service import (
    ResearchArtifactRetentionService,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


class PageUsageEntry(TypedDict):
    name: str
    bytes: int


class DiagnosticWaitPlan(TypedDict):
    retention_days: int
    decisions: int
    note: str
    est_freed_bytes: int


_SIZE_TABLES = (
    "watchlist_quant_v6_artifacts",
    "watchlist_quant_v6_publication_artifacts",
    "strategy_v2_forward_replay_artifacts",
    "strategy_v2_forward_evidence_artifacts",
    "strategy_v2_shadow_decisions",
)


def _engine_for(database_url: str) -> Engine:
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
        finally:
            cursor.close()

    return engine


def _sqlite_path(database_url: str) -> Path | None:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix):
        return None
    return Path(database_url[len(prefix):]).resolve()


def _page_usage(engine: Engine) -> tuple[list[PageUsageEntry], bool]:
    with engine.connect() as connection:
        try:
            rows = connection.execute(
                text(
                    "SELECT name, SUM(pgsize) FROM dbstat "
                    "GROUP BY name ORDER BY 2 DESC"
                )
            ).all()
        except Exception:
            return [], False
        usage: list[PageUsageEntry] = []
        for name, size in rows:
            if not str(name).startswith("sqlite_"):
                usage.append({"name": str(name), "bytes": int(size or 0)})
        return usage, True


def _row_counts(engine: Engine, names: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    with engine.connect() as connection:
        for name in names:
            counts[name] = int(
                connection.execute(
                    text(f"SELECT COUNT(*) FROM {name}")
                ).scalar_one()
            )
    return counts


def _retention_window_enabled(retention_days: int) -> bool:
    if retention_days < 0:
        raise ValueError("retention_days must be non-negative")
    return retention_days > 0


def _quant_v6_plan(
    session: Session,
    *,
    retention_days: int,
    now: datetime,
) -> dict[str, int]:
    if not _retention_window_enabled(retention_days):
        return {
            "retention_days": retention_days,
            "expired_publications": 0,
            "bindings": 0,
            "artifacts": 0,
        }
    cutoff = now - timedelta(days=retention_days)
    expired_publications = (
        session.query(WatchlistQuantV6Publication)
        .filter(WatchlistQuantV6Publication.published_at < cutoff)
        .count()
    )
    bindings = (
        session.query(WatchlistQuantV6PublicationArtifact)
        .join(
            WatchlistQuantV6Publication,
            WatchlistQuantV6Publication.id
            == WatchlistQuantV6PublicationArtifact.publication_id,
        )
        .filter(WatchlistQuantV6Publication.published_at < cutoff)
        .count()
    )
    surviving = (
        session.query(WatchlistQuantV6PublicationArtifact.artifact_sha256)
        .join(
            WatchlistQuantV6Publication,
            WatchlistQuantV6Publication.id
            == WatchlistQuantV6PublicationArtifact.publication_id,
        )
        .filter(WatchlistQuantV6Publication.published_at >= cutoff)
    )
    artifacts = (
        session.query(WatchlistQuantV6Artifact)
        .filter(
            WatchlistQuantV6Artifact.created_at < cutoff,
            WatchlistQuantV6Artifact.digest_sha256.not_in(surviving),
        )
        .count()
    )
    return {
        "retention_days": retention_days,
        "expired_publications": int(expired_publications),
        "bindings": int(bindings),
        "artifacts": int(artifacts),
    }


def _forward_replay_plan(
    session: Session,
    *,
    retention_days: int,
    now: datetime,
) -> dict[str, int]:
    if not _retention_window_enabled(retention_days):
        return {
            "retention_days": retention_days,
            "expired_evidence": 0,
            "bindings": 0,
            "artifacts": 0,
        }
    cutoff = now - timedelta(days=retention_days)
    expired_evidence = (
        session.query(StrategyV2ForwardEvidence)
        .filter(StrategyV2ForwardEvidence.evaluated_at < cutoff)
        .count()
    )
    bindings = (
        session.query(StrategyV2ForwardEvidenceArtifact)
        .join(
            StrategyV2ForwardEvidence,
            StrategyV2ForwardEvidence.id
            == StrategyV2ForwardEvidenceArtifact.evidence_id,
        )
        .filter(StrategyV2ForwardEvidence.evaluated_at < cutoff)
        .count()
    )
    surviving = (
        session.query(StrategyV2ForwardEvidenceArtifact.artifact_sha256)
        .join(
            StrategyV2ForwardEvidence,
            StrategyV2ForwardEvidence.id
            == StrategyV2ForwardEvidenceArtifact.evidence_id,
        )
        .filter(StrategyV2ForwardEvidence.evaluated_at >= cutoff)
    )
    artifacts = (
        session.query(StrategyV2ForwardReplayArtifact)
        .filter(
            StrategyV2ForwardReplayArtifact.created_at < cutoff,
            StrategyV2ForwardReplayArtifact.digest_sha256.not_in(surviving),
        )
        .count()
    )
    return {
        "retention_days": retention_days,
        "expired_evidence": int(expired_evidence),
        "bindings": int(bindings),
        "artifacts": int(artifacts),
    }


def _diagnostic_wait_plan(
    session: Session,
    *,
    retention_days: int,
    now: datetime,
) -> DiagnosticWaitPlan:
    if not _retention_window_enabled(retention_days):
        return {
            "retention_days": retention_days,
            "decisions": 0,
            "note": (
                "forward replay source protection is not evaluated in preview; "
                "apply may delete fewer rows"
            ),
            "est_freed_bytes": 0,
        }
    cutoff = now - timedelta(days=retention_days)
    decisions = (
        session.query(StrategyV2ShadowDecision)
        .filter(
            StrategyV2ShadowDecision.action == "WAIT",
            StrategyV2ShadowDecision.bar_at < cutoff,
        )
        .count()
    )
    return {
        "retention_days": retention_days,
        "decisions": int(decisions),
        "note": (
            "forward replay source protection is not evaluated in preview; "
            "apply may delete fewer rows"
        ),
        "est_freed_bytes": 0,
    }


def _shadow_decision_composition(session: Session) -> dict[str, int]:
    decisions = StrategyV2ShadowDecision
    total = session.query(decisions).count()
    non_wait = (
        session.query(decisions).filter(decisions.action != "WAIT").count()
    )
    gate_passed_wait = (
        session.query(decisions)
        .filter(decisions.action == "WAIT", decisions.gate_passed.is_(True))
        .count()
    )
    armed_wait = (
        session.query(decisions)
        .filter(
            decisions.action == "WAIT",
            decisions.gate_passed.is_(False),
            decisions.breach_armed.is_(True),
        )
        .count()
    )
    transition_wait = (
        session.query(decisions)
        .filter(
            decisions.action == "WAIT",
            decisions.gate_passed.is_(False),
            decisions.breach_armed.is_(False),
            decisions.state_before != decisions.state_after,
        )
        .count()
    )
    incomplete_wait = (
        session.query(decisions)
        .filter(
            decisions.action == "WAIT",
            decisions.reason == "SESSION_DATA_INCOMPLETE",
        )
        .count()
    )
    unarchived_evidence = (
        session.query(StrategyV2ForwardEvidence)
        .outerjoin(
            StrategyV2ForwardEvidenceArtifact,
            StrategyV2ForwardEvidenceArtifact.evidence_id
            == StrategyV2ForwardEvidence.id,
        )
        .filter(
            StrategyV2ForwardEvidence.disposition == "INCLUDED",
            StrategyV2ForwardEvidenceArtifact.evidence_id.is_(None),
        )
        .count()
    )
    return {
        "total": int(total),
        "non_wait_actions": int(non_wait),
        "gate_passed_wait": int(gate_passed_wait),
        "breach_armed_wait": int(armed_wait),
        "state_transition_wait": int(transition_wait),
        "incomplete_session_wait": int(incomplete_wait),
        "included_evidence_without_replay_artifact": int(unarchived_evidence),
    }


def _estimate_freed(
    plan_rows: int,
    *,
    table: str,
    usage_bytes: dict[str, int],
    row_counts: dict[str, int],
) -> int:
    table_rows = row_counts.get(table, 0)
    if table_rows <= 0 or plan_rows <= 0:
        return 0
    return int(usage_bytes.get(table, 0) * min(plan_rows, table_rows) / table_rows)


def _is_backup_file(name: str) -> bool:
    return ".db" in name or ".sqlite" in name


def _backup_plan(
    *,
    db_path: Path | None,
    backup_dir: Path,
    backup_dest: Path,
    keep: int,
) -> dict[str, object]:
    plan: dict[str, object] = {
        "backup_dir": str(backup_dir),
        "dest": str(backup_dest),
        "keep": keep,
        "move": [],
        "delete": [],
        "kept": [],
        "applied": False,
    }
    if db_path is not None:
        live_dir = db_path.parent.resolve()
        if backup_dir.resolve() == live_dir:
            raise ValueError(
                "backup directory must not be the live database directory"
            )
        if backup_dest.resolve() == live_dir:
            raise ValueError(
                "backup destination must not be the live database directory"
            )
    if not backup_dir.is_dir():
        return plan
    candidates = sorted(
        (
            path
            for path in backup_dir.iterdir()
            if path.is_file() and _is_backup_file(path.name)
        ),
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    plan["move"] = [
        {
            "file": path.name,
            "bytes": path.stat().st_size,
            "dest": str(backup_dest / path.name),
        }
        for path in candidates
    ]
    existing_dest: list[Path] = []
    if backup_dest.is_dir():
        existing_dest = [
            path
            for path in backup_dest.iterdir()
            if path.is_file() and _is_backup_file(path.name)
        ]
    combined = sorted(
        [*existing_dest, *candidates],
        key=lambda path: (path.stat().st_mtime, path.name),
        reverse=True,
    )
    seen: set[str] = set()
    unique: list[Path] = []
    for path in combined:
        if path.name in seen:
            continue
        seen.add(path.name)
        unique.append(path)
    plan["kept"] = [path.name for path in unique[:keep]]
    plan["delete"] = [
        {"file": path.name, "bytes": path.stat().st_size}
        for path in unique[keep:]
    ]
    return plan


def _apply_backup_plan(
    plan: dict[str, object],
    *,
    backup_dir: Path,
    backup_dest: Path,
) -> None:
    backup_dest.mkdir(parents=True, exist_ok=True)
    for entry in plan["move"]:  # type: ignore[union-attr]
        source = backup_dir / entry["file"]
        target = backup_dest / entry["file"]
        if target.exists():
            continue
        shutil.move(str(source), str(target))
    for entry in plan["delete"]:  # type: ignore[union-attr]
        stale = backup_dest / entry["file"]
        if stale.is_file():
            stale.unlink()


def _vacuum_guard(now: datetime) -> str | None:
    open_markets = [
        market
        for market in supported_markets()
        if is_trading_hours(market, now)
    ]
    if open_markets:
        return (
            "VACUUM refused during regular trading hours "
            f"({', '.join(open_markets)}); rerun outside HK and US market "
            "hours"
        )
    return None


def _vacuum_sqlite(engine: Engine, db_path: Path) -> None:
    free_bytes = shutil.disk_usage(db_path.parent).free
    db_bytes = db_path.stat().st_size
    if free_bytes < db_bytes:
        raise RuntimeError(
            "VACUUM refused: free disk space "
            f"({free_bytes} bytes) is below the database size "
            f"({db_bytes} bytes); VACUUM rewrites the whole file"
        )
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:
        checkpoint = connection.exec_driver_sql(
            "PRAGMA wal_checkpoint(TRUNCATE)"
        ).one()
        busy = int(checkpoint[0])
        if busy != 0:
            raise RuntimeError(
                "SQLite WAL checkpoint is busy; VACUUM was not run"
            )
        connection.exec_driver_sql("VACUUM")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Preview per-table page usage, retention impact, VACUUM safety, "
            "and backup relocation. Mutation requires --apply."
        )
    )
    parser.add_argument(
        "--database-url",
        default=settings.database_url,
        help="SQLAlchemy database URL (default: configured AUTO_TRADE_DATABASE_URL)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="perform the retention prune and backup relocation; default is preview",
    )
    parser.add_argument(
        "--vacuum",
        action="store_true",
        help=(
            "with --apply: checkpoint the WAL and VACUUM. Refused during "
            "any market's regular trading hours or when free disk space is "
            "below the database size"
        ),
    )
    parser.add_argument(
        "--backup-dir",
        help="directory holding .db backup copies (default: <db dir>/backups)",
    )
    parser.add_argument(
        "--backup-dest",
        help=(
            "relocation destination OUTSIDE the live database directory "
            "(default: sibling 'backups' of the live database directory)"
        ),
    )
    parser.add_argument(
        "--backup-keep",
        type=int,
        default=5,
        help="rolling number of newest backups to keep at the destination",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.backup_keep < 1:
        parser.error("--backup-keep must be at least 1")
    if args.vacuum and not args.apply:
        parser.error("--vacuum requires --apply")

    db_path = _sqlite_path(args.database_url)
    default_dir = db_path.parent if db_path is not None else Path("data")
    backup_dir = Path(args.backup_dir) if args.backup_dir else (
        default_dir / "backups"
    )
    backup_dest = Path(args.backup_dest) if args.backup_dest else (
        default_dir.parent / "backups"
    )
    now = datetime.now(timezone.utc)

    if args.vacuum:
        if db_path is None:
            print(
                "error: --vacuum is supported only for SQLite databases",
                file=sys.stderr,
            )
            return 2
        refusal = _vacuum_guard(now)
        if refusal is not None:
            print(f"error: {refusal}", file=sys.stderr)
            return 2

    try:
        relocation = _backup_plan(
            db_path=db_path,
            backup_dir=backup_dir,
            backup_dest=backup_dest,
            keep=args.backup_keep,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    engine = _engine_for(args.database_url)
    try:
        page_usage, page_usage_available = _page_usage(engine)
        row_counts = _row_counts(engine, _SIZE_TABLES)
        with Session(bind=engine) as session:
            quant_v6_plan = _quant_v6_plan(
                session,
                retention_days=settings.watchlist_quant_v6_artifact_retention_days,
                now=now,
            )
            replay_plan = _forward_replay_plan(
                session,
                retention_days=(
                    settings.strategy_v2_forward_replay_artifact_retention_days
                ),
                now=now,
            )
            diagnostic_plan = _diagnostic_wait_plan(
                session,
                retention_days=settings.strategy_v2_diagnostic_wait_retention_days,
                now=now,
            )
            composition = _shadow_decision_composition(session)

        usage_bytes = {
            str(entry["name"]): int(entry["bytes"]) for entry in page_usage
        }
        quant_v6_freed = _estimate_freed(
            int(quant_v6_plan["artifacts"]),
            table="watchlist_quant_v6_artifacts",
            usage_bytes=usage_bytes,
            row_counts=row_counts,
        ) + _estimate_freed(
            int(quant_v6_plan["bindings"]),
            table="watchlist_quant_v6_publication_artifacts",
            usage_bytes=usage_bytes,
            row_counts=row_counts,
        )
        replay_freed = _estimate_freed(
            int(replay_plan["artifacts"]),
            table="strategy_v2_forward_replay_artifacts",
            usage_bytes=usage_bytes,
            row_counts=row_counts,
        ) + _estimate_freed(
            int(replay_plan["bindings"]),
            table="strategy_v2_forward_evidence_artifacts",
            usage_bytes=usage_bytes,
            row_counts=row_counts,
        )
        diagnostic_freed = _estimate_freed(
            int(diagnostic_plan["decisions"]),
            table="strategy_v2_shadow_decisions",
            usage_bytes=usage_bytes,
            row_counts=row_counts,
        )
        quant_v6_plan["est_freed_bytes"] = quant_v6_freed
        replay_plan["est_freed_bytes"] = replay_freed
        diagnostic_plan["est_freed_bytes"] = diagnostic_freed

        current_bytes = (
            db_path.stat().st_size
            if db_path is not None and db_path.is_file()
            else sum(usage_bytes.values())
        )
        est_freed = quant_v6_freed + replay_freed + diagnostic_freed

        applied: dict[str, object] | None = None
        if args.apply:
            with Session(bind=engine) as session:
                artifact_retention = ResearchArtifactRetentionService(session)
                quant_v6_result = (
                    artifact_retention.prune_expired_quant_v6_publication_payloads(
                        retention_days=(
                            settings.watchlist_quant_v6_artifact_retention_days
                        ),
                        batch_size=(
                            settings.watchlist_quant_v6_artifact_maintenance_batch_size
                        ),
                        max_batches=None,
                        now=now,
                    )
                )
                replay_result = (
                    artifact_retention.prune_expired_forward_replay_artifacts(
                        retention_days=(
                            settings.strategy_v2_forward_replay_artifact_retention_days
                        ),
                        batch_size=(
                            settings.strategy_v2_forward_replay_artifact_maintenance_batch_size
                        ),
                        max_batches=None,
                        now=now,
                    )
                )
                diagnostic_result = StrategyV2ShadowService(
                    session
                ).prune_expired_diagnostic_wait_decisions(
                    retention_days=(
                        settings.strategy_v2_diagnostic_wait_retention_days
                    ),
                    batch_size=(
                        settings.strategy_v2_diagnostic_wait_maintenance_batch_size
                    ),
                    max_batches=None,
                    now=now,
                    replay_source_retention_days=(
                        settings.strategy_v2_forward_replay_artifact_retention_days
                        or None
                    ),
                )
            _apply_backup_plan(
                relocation,
                backup_dir=backup_dir,
                backup_dest=backup_dest,
            )
            relocation["applied"] = True
            applied = {
                "watchlist_quant_v6": {
                    "bindings_deleted": quant_v6_result.bindings_deleted,
                    "artifacts_deleted": quant_v6_result.artifacts_deleted,
                    "batches": quant_v6_result.batches,
                },
                "strategy_v2_forward_replay": {
                    "bindings_deleted": replay_result.bindings_deleted,
                    "artifacts_deleted": replay_result.artifacts_deleted,
                    "batches": replay_result.batches,
                },
                "strategy_v2_diagnostic_wait": {
                    "deleted": diagnostic_result.deleted,
                    "batches": diagnostic_result.batches,
                },
            }

        vacuumed = False
        if args.vacuum:
            assert db_path is not None
            try:
                _vacuum_sqlite(engine, db_path)
            except RuntimeError as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            vacuumed = True
    finally:
        engine.dispose()

    payload = {
        "mode": "APPLY" if args.apply else "PREVIEW",
        "database": str(db_path) if db_path is not None else args.database_url,
        "generated_at": now.isoformat(),
        "page_usage_available": page_usage_available,
        "page_usage": page_usage,
        "row_counts": row_counts,
        "retention": {
            "watchlist_quant_v6": quant_v6_plan,
            "strategy_v2_forward_replay": replay_plan,
            "strategy_v2_diagnostic_wait": diagnostic_plan,
        },
        "shadow_decision_composition": composition,
        "projection": {
            "current_bytes": current_bytes,
            "est_freed_bytes": est_freed,
            "projected_bytes": max(0, current_bytes - est_freed),
            "note": (
                "freed pages return to SQLite's free list; file size only "
                "shrinks after VACUUM (outside market hours)"
            ),
        },
        "applied": applied,
        "vacuum": {
            "requested": bool(args.vacuum),
            "applied": vacuumed,
            "market_hours_guard": "refuses during any market RTH",
        },
        "backup_relocation": relocation,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
