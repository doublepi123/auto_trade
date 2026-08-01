"""Read-only browse service over the append-only ``AuditLog`` table.

Pure observability surface: filters, bounded limit/offset pagination and a
deterministic newest-first order. Never mutates audit rows and never writes new
ones (the browse endpoint is deliberately not self-auditing).
"""
from __future__ import annotations

import json
from datetime import date, datetime, time, timezone
from typing import Any

from sqlalchemy import desc, func, select
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

        Reuses the exact ``list_logs`` filter semantics (action/severity exact
        upper-cased match, inclusive UTC day boundaries). Returns the filtered
        population ``total`` plus deterministic aggregations:

        * ``by_action`` / ``by_severity`` / ``by_actor`` — categorical, bounded
          with truthful ``*_other_total`` overflow; ordered count desc then key
          asc so ties are deterministic.
        * ``by_day`` — UTC day, unbounded and chronological.

        The sum contract is testable: for each categorical dimension,
        ``sum(bucket.count) + other_total == total``; daily counts always sum
        exactly to ``total``. Never exposes raw API keys, IPs, actor material,
        payload bodies, or exception text — only the pseudonymous ``actor_hash``
        already persisted on each row.
        """
        from_dt, to_dt = _validate_date_range(from_date, to_date)

        def _filtered(statement):  # noqa: ANN001
            return _apply_filters(
                statement,
                action=action,
                severity=severity,
                from_dt=from_dt,
                to_dt=to_dt,
            )

        total = int(
            self._db.scalar(_filtered(select(func.count()).select_from(AuditLog)))
            or 0
        )

        action_rows, action_other = self._categorical_buckets(
            dimension=AuditLog.action,
            cap=_MAX_ACTION_BUCKETS,
            filtered=_filtered,
        )
        by_action = [
            AuditLogCategoryCount(key=key, count=count)
            for key, count in action_rows
        ]

        severity_rows, severity_other = self._categorical_buckets(
            dimension=AuditLog.severity,
            cap=_MAX_SEVERITY_BUCKETS,
            filtered=_filtered,
        )
        by_severity = [
            AuditLogCategoryCount(key=key, count=count)
            for key, count in severity_rows
        ]

        actor_rows, actor_other = self._categorical_buckets(
            dimension=AuditLog.actor_hash,
            cap=_MAX_ACTOR_BUCKETS,
            filtered=_filtered,
        )
        by_actor = [
            AuditLogActorCount(actor_hash=key, count=count)
            for key, count in actor_rows
        ]

        by_day = self._daily_buckets(filtered=_filtered)

        return AuditLogStatsResponse(
            total=total,
            by_action=by_action,
            action_other_total=action_other,
            by_severity=by_severity,
            severity_other_total=severity_other,
            by_actor=by_actor,
            actor_other_total=actor_other,
            by_day=by_day,
            filters=self._filters_snapshot(
                action=action,
                severity=severity,
                from_date=from_date,
                to_date=to_date,
            ),
        )

    def _categorical_buckets(
        self,
        *,
        dimension,
        cap: int,
        filtered,
    ) -> tuple[list[tuple[str, int]], int]:
        """Aggregate by ``dimension`` with count-desc/key-asc ordering.

        Returns the top-``cap`` buckets plus the truthful overflow total
        (``other_total``) so callers can verify the sum contract. ``dimension``
        is a SQLAlchemy column; ``filtered`` is the shared filter wrapper.
        """
        raw = self._db.execute(
            filtered(
                select(dimension, func.count()).group_by(dimension)
            )
        ).all()
        # Stable order: count desc then key asc.
        ordered = sorted(raw, key=lambda row: (-int(row[1] or 0), str(row[0])))
        kept = ordered[:cap]
        other_total = sum(int(count or 0) for _, count in ordered[cap:])
        return [(str(key), int(count or 0)) for key, count in kept], other_total

    def _daily_buckets(self, *, filtered) -> list[AuditLogDayCount]:
        """Aggregate by UTC day, chronological order (unbounded)."""
        day_expression = func.date(AuditLog.created_at)
        raw = self._db.execute(
            filtered(
                select(day_expression, func.count()).group_by(day_expression)
            )
        ).all()
        ordered = sorted(raw, key=lambda row: str(row[0]))
        return [
            AuditLogDayCount(
                day=date.fromisoformat(str(row[0])),
                count=int(row[1] or 0),
            )
            for row in ordered
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
            snapshot["action"] = action.strip().upper()
        if severity:
            snapshot["severity"] = severity.strip().upper()
        if from_date is not None:
            snapshot["from_date"] = from_date.isoformat()
        if to_date is not None:
            snapshot["to_date"] = to_date.isoformat()
        return snapshot
