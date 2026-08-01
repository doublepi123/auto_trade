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
    NotificationFailureCount,
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


def _validate_date_range(from_date: str | None, to_date: str | None) -> None:
    """Reject an inverted ``[from_date, to_date]`` range consistently.

    Both bounds, when present, are YYYY-MM-DD strings; ``from_date > to_date``
    is a client error (HTTP 422). Raises ``ValueError`` so the API layer can
    translate it to a 422 exactly like an unparseable date.
    """
    if from_date is None or to_date is None:
        return
    if _parse_date(from_date) > _parse_date(to_date):
        raise ValueError("from_date must be on or before to_date")


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


# Channel labels MultiChannelNotifier records in ``error`` as a
# "{ClassName}: {detail}" prefix when a channel fails. Only these known labels
# are surfaced in the failure attribution; failed rows with any other (or
# empty) error prefix are bucketed under ``unknown``. Successful rows are never
# attributed — the log does not persist which channel carried them — so they
# are excluded from ``failures_by_channel`` entirely.
#
# The match is anchored to the exact ``ClassName: `` prefix (note the colon and
# space) so a string like ``ServerChanNotifierXYZ: foo`` is NOT mis-attributed
# to ``serverchan``.
_NOTIFIER_CLASS_CHANNELS: dict[str, str] = {
    "ServerChanNotifier: ": "serverchan",
    "WebhookNotifier: ": "webhook",
    "TelegramNotifier: ": "telegram",
}

_CHANNEL_CASE = case(
    *[
        (NotificationLog.error.like(f"{prefix}%"), label)
        for prefix, label in _NOTIFIER_CLASS_CHANNELS.items()
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
        _validate_date_range(from_date, to_date)
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
        _validate_date_range(from_date, to_date)
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

        Read-only aggregation — never exposes title/content/error payloads.

        ``failures_by_channel`` is a *failure-only* attribution. The
        ``NotificationLog`` table does not persist which channel carried a
        successful send, so a per-channel total/success/failed bucket would be
        misleading. Instead this counts only failed rows: known notifier class
        prefixes in ``error`` (``ServerChanNotifier: ``, ``WebhookNotifier: ``,
        ``TelegramNotifier: ``) are attributed to ``serverchan`` / ``webhook`` /
        ``telegram``; failed rows with no recognized prefix are ``unknown``.
        Successful rows are excluded entirely. The sum of all
        ``failures_by_channel`` counts equals the response's ``failed`` total.
        """
        _validate_date_range(from_date, to_date)

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

        # Failure-only channel attribution: count failed rows grouped by the
        # recognized notifier-class prefix in ``error`` (or ``unknown``).
        # Successful rows are excluded entirely — the log does not persist
        # which channel carried them, so attributing them would be misleading.
        failure_rows = self._db.execute(
            _filtered(
                select(_CHANNEL_CASE, func.count(NotificationLog.id))
                .where(NotificationLog.success.is_(False))
            ).group_by(_CHANNEL_CASE).order_by(_CHANNEL_CASE)
        )
        failures_by_channel = [
            NotificationFailureCount(key=str(key), count=int(count))
            for key, count in failure_rows
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
            failures_by_channel=failures_by_channel,
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
