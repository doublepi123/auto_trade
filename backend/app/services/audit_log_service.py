"""Read-only browse service over the append-only ``AuditLog`` table.

Pure observability surface: filters, bounded limit/offset pagination and a
deterministic newest-first order. Never mutates audit rows and never writes new
ones (the browse endpoint is deliberately not self-auditing).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import Engine, desc, func, select
from sqlalchemy.engine import Connection
from sqlalchemy.orm import Session

from app.models import AuditLog
from app.schemas import (
    AuditLogActorCount,
    AuditLogCategoryCount,
    AuditLogDayCount,
    AuditLogOut,
    AuditLogPage,
    AuditLogStatsResponse,
)

# Categorical aggregation caps. Daily rows are unbounded (chronological); the
# categorical dimensions (action / severity / actor) are bounded and report
# overflow truthfully via ``*_other_total`` so the sum contract holds.
_MAX_ACTION_BUCKETS = 50
_MAX_SEVERITY_BUCKETS = 16
_MAX_ACTOR_BUCKETS = 25


def _parse_request_summary(value: str) -> dict[str, Any]:
    """Parse the stored summary; non-JSON rows degrade to ``{"raw": ...}``."""
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": value}


def _validate_date_range(
    from_date: date | None,
    to_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    """Shared filter semantics: inclusive UTC day boundaries.

    Raises ``ValueError`` when ``from_date`` is after ``to_date``. Mirrors the
    interpretation used by ``list_logs`` so the stats endpoint cannot drift
    into a second interpretation of the same filters.
    """
    if from_date is not None and to_date is not None and from_date > to_date:
        raise ValueError("from_date must be on or before to_date")
    from_dt = (
        datetime.combine(from_date, time.min, tzinfo=timezone.utc)
        if from_date is not None
        else None
    )
    to_dt = (
        datetime.combine(to_date, time.max, tzinfo=timezone.utc)
        if to_date is not None
        else None
    )
    return from_dt, to_dt


def _apply_filters(
    statement,
    *,
    action: str | None,
    severity: str | None,
    from_dt: datetime | None,
    to_dt: datetime | None,
):  # noqa: ANN001, ANN202
    s = statement
    if action:
        s = s.where(AuditLog.action == action.strip().upper())
    if severity:
        s = s.where(AuditLog.severity == severity.strip().upper())
    if from_dt is not None:
        s = s.where(AuditLog.created_at >= from_dt)
    if to_dt is not None:
        s = s.where(AuditLog.created_at <= to_dt)
    return s


class AuditLogService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def list_logs(
        self,
        *,
        action: str | None = None,
        severity: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> AuditLogPage:
        """Newest-first audit rows with safe filters and bounded pagination.

        Raises ``ValueError`` when ``from_date`` is after ``to_date``.
        """
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))
        from_dt, to_dt = _validate_date_range(from_date, to_date)

        def _filtered(statement):  # noqa: ANN001
            return _apply_filters(
                statement,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
            )

        total = self._db.scalar(
            _filtered(select(func.count()).select_from(AuditLog))
        ) or 0
        rows = self._db.scalars(
            _filtered(select(AuditLog))
            .order_by(desc(AuditLog.created_at), desc(AuditLog.id))
            .limit(limit)
            .offset(offset)
        )
        items = [
            AuditLogOut(
                id=r.id,
                action=r.action,
                severity=r.severity,
                result=r.result,
                actor_hash=r.actor_hash,
                source_ip=r.source_ip,
                request_summary=_parse_request_summary(r.request_summary),
                created_at=r.created_at,
            )
            for r in rows
        ]
        return AuditLogPage(items=items, total=total, limit=limit, offset=offset)

    def stats(
        self,
        *,
        action: str | None = None,
        severity: str | None = None,
        from_date: date | None = None,
        to_date: date | None = None,
    ) -> AuditLogStatsResponse:
        """Read-only aggregations over the filtered audit-log population.

        All aggregates (total, categorical, actor, severity, daily) execute
        inside ONE explicit SQLite read snapshot so a concurrent commit
        between aggregate queries cannot split the population. The snapshot
        uses a dedicated short-lived read connection bound to the same engine
        as the caller's session, preserving existing committed-row semantics.
        The caller's request Session is never committed/rolled back or
        otherwise mutated.

        Reuses the exact ``list_logs`` filter semantics (action/severity exact
        upper-cased match, inclusive UTC day boundaries). Categorical/actor
        groups are ordered and limited in SQL (count desc then key asc); each
        overflow/other total is derived as ``filtered_total - sum(kept)``, so
        the sum contract holds even with null/unknown buckets. Daily buckets
        are chronological and bounded by the validated date range.

        Never exposes raw API keys, IPs, actor material, payload bodies, or
        exception text — only the pseudonymous ``actor_hash`` already
        persisted on each row.
        """
        from_dt, to_dt = _validate_date_range(from_date, to_date)
        normalized_action = action.strip().upper() if action else None
        normalized_severity = (
            severity.strip().upper() if severity else None
        )

        return self._run_snapshot(
            action=normalized_action,
            severity=normalized_severity,
            from_dt=from_dt,
            to_dt=to_dt,
            from_date=from_date,
            to_date=to_date,
        )

    def _run_snapshot(
        self,
        *,
        action: str | None,
        severity: str | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
        from_date: date | None,
        to_date: date | None,
    ) -> AuditLogStatsResponse:
        """Execute all aggregates inside one SQLite read snapshot.

        Opens a dedicated short-lived connection off the caller's engine,
        issues ``BEGIN`` (a read snapshot in SQLite WAL), runs every aggregate
        against that one snapshot, then releases it. The caller's request
        Session is untouched (no commit/rollback/mutation).
        """
        bind = self._db.get_bind()
        if isinstance(bind, Engine):
            with bind.connect() as connection:
                return self._run_snapshot_on_connection(
                    connection=connection,
                    action=action,
                    severity=severity,
                    from_dt=from_dt,
                    to_dt=to_dt,
                    from_date=from_date,
                    to_date=to_date,
                )
        # bind is already a Connection (e.g. inside an existing transaction).
        return self._run_snapshot_on_connection(
            connection=bind,
            action=action,
            severity=severity,
            from_dt=from_dt,
            to_dt=to_dt,
            from_date=from_date,
            to_date=to_date,
        )

    def _run_snapshot_on_connection(
        self,
        *,
        connection: Connection,
        action: str | None,
        severity: str | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
        from_date: date | None,
        to_date: date | None,
    ) -> AuditLogStatsResponse:
        # One explicit read snapshot. In SQLite WAL, BEGIN defers the write
        # lock and establishes a read snapshot; all SELECTs below observe the
        # same committed state. We never COMMIT/ROLLBACK this connection, so
        # nothing is mutated.
        driver_connection = connection.connection
        try:
            driver_connection.rollback()
        except Exception:
            pass
        try:
            driver_connection.execute("BEGIN")
        except Exception:
            # Already in a transaction (e.g. a passed-in connection); the
            # subsequent SELECTs still share one snapshot.
            pass

        try:
            total = int(
                connection.scalar(
                    _apply_filters(
                        select(func.count()).select_from(AuditLog),
                        action=action,
                        severity=severity,
                        from_dt=from_dt,
                        to_dt=to_dt,
                    )
                )
                or 0
            )

            action_rows = self._sql_categorical(
                connection=connection,
                dimension=AuditLog.action,
                cap=_MAX_ACTION_BUCKETS,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
                total=total,
            )
            by_action = [
                AuditLogCategoryCount(key=key, count=count)
                for key, count in action_rows
            ]

            severity_rows = self._sql_categorical(
                connection=connection,
                dimension=AuditLog.severity,
                cap=_MAX_SEVERITY_BUCKETS,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
                total=total,
            )
            by_severity = [
                AuditLogCategoryCount(key=key, count=count)
                for key, count in severity_rows
            ]

            actor_rows = self._sql_categorical(
                connection=connection,
                dimension=AuditLog.actor_hash,
                cap=_MAX_ACTOR_BUCKETS,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
                total=total,
            )
            by_actor = [
                AuditLogActorCount(actor_hash=key, count=count)
                for key, count in actor_rows
            ]

            by_day = self._sql_daily(
                connection=connection,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
            )
        finally:
            try:
                driver_connection.rollback()
            except Exception:
                pass

        return AuditLogStatsResponse(
            total=total,
            by_action=by_action,
            action_other_total=self._overflow(total, by_action),
            by_severity=by_severity,
            severity_other_total=self._overflow(total, by_severity),
            by_actor=by_actor,
            actor_other_total=self._overflow(total, by_actor),
            by_day=by_day,
            filters=self._filters_snapshot(
                action=action,
                severity=severity,
                from_date=from_date,
                to_date=to_date,
            ),
        )

    @staticmethod
    def _overflow(
        total: int,
        kept: list[AuditLogCategoryCount] | list[AuditLogActorCount],
    ) -> int:
        """Derive overflow as filtered total minus the sum of kept buckets.

        This is truthful by construction: it accounts for null/unknown and
        truncated buckets alike, so conservation always holds.
        """
        kept_sum = sum(int(b.count) for b in kept)
        return max(0, total - kept_sum)

    def _sql_categorical(
        self,
        *,
        connection: Connection,
        dimension,
        cap: int,
        action: str | None,
        severity: str | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
        total: int,
    ) -> list[tuple[str, int]]:
        """Order and limit a categorical aggregation in SQL.

        Returns the top-``cap`` buckets ordered count desc then key asc. The
        caller derives the overflow total from ``total - sum(kept)`` so we do
        not need to materialize all distinct keys merely to truncate.
        """
        if total == 0:
            return []
        stmt = _apply_filters(
            select(dimension, func.count())
            .group_by(dimension)
            .order_by(desc(func.count()), dimension)
            .limit(cap),
            action=action,
            severity=severity,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        raw = connection.execute(stmt).all()
        return [(str(key), int(count or 0)) for key, count in raw]

    def _sql_daily(
        self,
        *,
        connection: Connection,
        action: str | None,
        severity: str | None,
        from_dt: datetime | None,
        to_dt: datetime | None,
    ) -> list[AuditLogDayCount]:
        """Aggregate by UTC day, chronological order, bounded by date range."""
        day_expression = func.date(AuditLog.created_at)
        stmt = _apply_filters(
            select(day_expression, func.count())
            .group_by(day_expression)
            .order_by(day_expression),
            action=action,
            severity=severity,
            from_dt=from_dt,
            to_dt=to_dt,
        )
        raw = connection.execute(stmt).all()
        return [
            AuditLogDayCount(
                day=date.fromisoformat(str(row[0])),
                count=int(row[1] or 0),
            )
            for row in raw
            if row[0] is not None
        ]

    @staticmethod
    def _filters_snapshot(
        *,
        action: str | None,
        severity: str | None,
        from_date: date | None,
        to_date: date | None,
    ) -> dict[str, Any]:
        """Echo the effective filters (normalized exactly as applied)."""
        snapshot: dict[str, Any] = {}
        if action:
            snapshot["action"] = action
        if severity:
            snapshot["severity"] = severity
        if from_date is not None:
            snapshot["from_date"] = from_date.isoformat()
        if to_date is not None:
            snapshot["to_date"] = to_date.isoformat()
        return snapshot
