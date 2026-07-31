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
from app.schemas import AuditLogOut, AuditLogPage


def _parse_request_summary(value: str) -> dict[str, Any]:
    """Parse the stored summary; non-JSON rows degrade to ``{"raw": ...}``."""
    if not value or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"raw": value}


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

        def _filtered(statement):  # noqa: ANN001
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
