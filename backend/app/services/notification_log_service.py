"""Notification dispatch log — persists every sent notification via a sink.

The sink is attached to ``MultiChannelNotifier.send`` (by the runner) so risk /
alert / report notifications become auditable. All logging is best-effort: the
sink never raises, so a logging failure can never block a real notification.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Callable, Optional

from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import NotificationLog
from app.schemas import NotificationLogOut, NotificationLogPage

logger = logging.getLogger(__name__)

# Callable shape MultiChannelNotifier.send invokes: (title, content, severity, success, error)
NotificationSink = Callable[[str, str, str, bool, str], None]


def _parse_date(value: str) -> datetime:
    """Parse a YYYY-MM-DD string into a UTC datetime."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


class NotificationLogService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def list_logs(
        self,
        *,
        severity: str | None = None,
        q: str | None = None,
        success: bool | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
        page: int = 1,
        page_size: int = 50,
    ) -> NotificationLogPage:
        page = max(1, page)
        page_size = max(1, min(page_size, 200))
        stmt = select(NotificationLog)
        count_stmt = select(func.count()).select_from(NotificationLog)

        def _apply_filters(statement):  # noqa: ANN202
            s = statement
            if severity:
                s = s.where(NotificationLog.severity == severity)
            if q:
                pattern = f"%{q}%"
                s = s.where(
                    or_(
                        NotificationLog.title.ilike(pattern),
                        NotificationLog.content.ilike(pattern),
                        NotificationLog.error.ilike(pattern),
                    )
                )
            if success is not None:
                s = s.where(NotificationLog.success.is_(success))
            if from_date:
                s = s.where(NotificationLog.created_at >= _parse_date(from_date))
            if to_date:
                end = datetime.combine(_parse_date(to_date).date(), time.max, tzinfo=timezone.utc)
                s = s.where(NotificationLog.created_at <= end)
            return s

        stmt = _apply_filters(stmt)
        count_stmt = _apply_filters(count_stmt)
        total = self._db.scalar(count_stmt) or 0
        stmt = (
            stmt.order_by(desc(NotificationLog.created_at), desc(NotificationLog.id))
            .limit(page_size)
            .offset((page - 1) * page_size)
        )
        rows = list(self._db.scalars(stmt))
        return NotificationLogPage(
            items=[NotificationLogOut.model_validate(r) for r in rows],
            total=total,
            page=page,
            page_size=page_size,
        )


class NotificationLogSink:
    """Best-effort persister attached to the notifier."""

    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self._sf = session_factory

    def record(
        self,
        title: str,
        content: str,
        severity: str,
        success: bool,
        error: str = "",
    ) -> None:
        try:
            db = self._sf()
            try:
                db.add(NotificationLog(
                    title=(title or "")[:200],
                    content=(content or "")[:2000],
                    severity=(severity or "INFO"),
                    success=bool(success),
                    error=(error or "")[:500],
                ))
                db.commit()
            finally:
                db.close()
        except Exception:
            logger.debug("notification log sink failed", exc_info=True)


_sink_singleton: Optional[NotificationLogSink] = None


def get_notification_sink() -> NotificationLogSink:
    """Module singleton bound to the app's SessionLocal."""
    global _sink_singleton
    if _sink_singleton is None:
        from app.database import SessionLocal
        _sink_singleton = NotificationLogSink(SessionLocal)
    return _sink_singleton
