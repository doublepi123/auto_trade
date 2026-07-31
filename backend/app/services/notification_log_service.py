"""Notification dispatch log — persists every sent notification via a sink.

The sink is attached to ``MultiChannelNotifier.send`` (by the runner) so risk /
alert / report notifications become auditable. All logging is best-effort: the
sink never raises, so a logging failure can never block a real notification.
"""
from __future__ import annotations

import logging
from datetime import datetime, time, timezone
from typing import Callable, Optional

from sqlalchemy import case, desc, func, or_, select
from sqlalchemy.orm import Session

from app.models import NotificationLog
from app.schemas import (
    NotificationDailyPoint,
    NotificationLogOut,
    NotificationLogPage,
    NotificationStatsBucket,
    NotificationStatsResponse,
)

logger = logging.getLogger(__name__)

# Callable shape MultiChannelNotifier.send invokes: (title, content, severity, success, error)
NotificationSink = Callable[[str, str, str, bool, str], None]


def _parse_date(value: str) -> datetime:
    """Parse a YYYY-MM-DD string into a UTC datetime."""
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)


def _apply_filters(
    statement,
    *,
    severity: str | None = None,
    q: str | None = None,
    success: bool | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
):  # noqa: ANN202
    """Apply the shared notification-log filters to a SELECT statement."""
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


# Channel labels MultiChannelNotifier records in ``error`` as a "{ClassName}: {detail}"
# prefix when a channel fails. Only these known labels are surfaced in the channel
# breakdown; anything else (including all successful rows, which carry no channel
# attribution) is bucketed under ``unknown``.
_NOTIFIER_CLASS_CHANNELS: dict[str, str] = {
    "ServerChanNotifier": "serverchan",
    "WebhookNotifier": "webhook",
    "TelegramNotifier": "telegram",
}

_CHANNEL_CASE = case(
    *[
        (NotificationLog.error.like(f"{cls}%"), label)
        for cls, label in _NOTIFIER_CLASS_CHANNELS.items()
    ],
    else_="unknown",
)


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
        stmt = _apply_filters(
            select(NotificationLog),
            severity=severity,
            q=q,
            success=success,
            from_date=from_date,
            to_date=to_date,
        )
        total = self._db.scalar(
            _apply_filters(
                select(func.count()).select_from(NotificationLog),
                severity=severity,
                q=q,
                success=success,
                from_date=from_date,
                to_date=to_date,
            )
        ) or 0
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

    def export_logs(
        self,
        *,
        severity: str | None = None,
        q: str | None = None,
        success: bool | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> list[NotificationLogOut]:
        """Return all matching notification rows for export (no pagination)."""
        stmt = _apply_filters(
            select(NotificationLog),
            severity=severity,
            q=q,
            success=success,
            from_date=from_date,
            to_date=to_date,
        )
        stmt = stmt.order_by(desc(NotificationLog.created_at), desc(NotificationLog.id))
        rows = list(self._db.scalars(stmt))
        return [NotificationLogOut.model_validate(r) for r in rows]

    def statistics(
        self,
        *,
        severity: str | None = None,
        from_date: str | None = None,
        to_date: str | None = None,
    ) -> NotificationStatsResponse:
        """Bounded, date-filtered delivery statistics over the notification log.

        Read-only aggregation — never exposes title/content/error payloads. The
        ``by_channel`` breakdown is derived from the only channel signal the log
        retains: the notifier class name MultiChannelNotifier prefixes into the
        ``error`` column when a channel fails. Successful rows (and unrecognized
        failures) are attributed to ``unknown``.
        """
        def _filtered(select_stmt):  # noqa: ANN001
            return _apply_filters(
                select_stmt,
                severity=severity,
                from_date=from_date,
                to_date=to_date,
            )

        total_row = self._db.execute(
            _filtered(
                select(
                    func.count(NotificationLog.id),
                    func.coalesce(
                        func.sum(case((NotificationLog.success.is_(True), 1), else_=0)),
                        0,
                    ),
                )
            )
        ).one()
        total = int(total_row[0])
        success = int(total_row[1])
        failed = total - success
        success_rate = round(success / total * 100.0, 2) if total else 0.0

        def _buckets(group_expr) -> list[NotificationStatsBucket]:  # noqa: ANN001
            rows = self._db.execute(
                _filtered(
                    select(
                        group_expr,
                        func.count(NotificationLog.id),
                        func.coalesce(
                            func.sum(case((NotificationLog.success.is_(True), 1), else_=0)),
                            0,
                        ),
                    )
                ).group_by(group_expr).order_by(group_expr)
            )
            return [
                NotificationStatsBucket(
                    key=str(key),
                    total=int(count),
                    success=int(ok),
                    failed=int(count) - int(ok),
                )
                for key, count, ok in rows
            ]

        day = func.date(NotificationLog.created_at)
        daily_rows = self._db.execute(
            _filtered(
                select(
                    day,
                    func.count(NotificationLog.id),
                    func.coalesce(
                        func.sum(case((NotificationLog.success.is_(True), 1), else_=0)),
                        0,
                    ),
                )
            ).group_by(day).order_by(day.asc())
        )
        return NotificationStatsResponse(
            from_date=from_date,
            to_date=to_date,
            total=total,
            success=success,
            failed=failed,
            success_rate=success_rate,
            by_severity=_buckets(NotificationLog.severity),
            by_channel=_buckets(_CHANNEL_CASE),
            daily=[
                NotificationDailyPoint(
                    date=str(date),
                    total=int(count),
                    success=int(ok),
                    failed=int(count) - int(ok),
                )
                for date, count, ok in daily_rows
            ],
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
