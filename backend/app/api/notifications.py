from __future__ import annotations

import csv
import io
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse

from app.api.auth import require_api_key
from app.database import get_db
from app.models import NotificationLog
from app.schemas import NotificationLogOut, NotificationLogPage
from app.services.credentials_service import CredentialsService
from app.services.notification_log_service import NotificationLogService
from app.core.notifiers.multi_channel import MultiChannelNotifier

router = APIRouter(
    prefix="/api/notifications",
    tags=["notifications"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=NotificationLogPage)
def list_notifications(
    severity: str | None = Query(default=None, description="Filter by severity"),
    q: str | None = Query(default=None, description="Search title/content/error (ILIKE)"),
    success: bool | None = Query(default=None, description="Filter by success"),
    from_date: str | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    to_date: str | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    db=Depends(get_db),
) -> NotificationLogPage:
    # Normalize severity to upper-case so `?severity=info` matches stored
    # 'INFO' values (matches the audit-log endpoint convention in trade.py);
    # without this a lower-case value silently returns zero rows.
    normalized = severity.strip().upper() if severity else None
    try:
        return NotificationLogService(db).list_logs(
            severity=normalized,
            q=q,
            success=success,
            from_date=from_date,
            to_date=to_date,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {exc}") from exc


@router.post("/{id}/retry", response_model=NotificationLogOut)
def retry_notification(id: int, db=Depends(get_db)) -> NotificationLogOut:
    """Re-send a previously logged notification using the current credentials.

    The original log row is updated in place with the new outcome so the
    notification center can immediately reflect the retry result.
    """
    log = db.get(NotificationLog, id)
    if log is None:
        raise HTTPException(status_code=404, detail="notification not found")

    config = CredentialsService(db).get_config()
    notifier = MultiChannelNotifier.from_credential_config(config)
    try:
        success = notifier.send(log.title, log.content, log.severity)
    finally:
        notifier.close()

    log.success = success
    log.error = "" if success else "retry failed"
    log.created_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(log)
    return NotificationLogOut.model_validate(log)


@router.get("/export")
def export_notifications(
    format: str = Query(default="csv", description="Export format: csv or json"),
    severity: str | None = Query(default=None, description="Filter by severity"),
    q: str | None = Query(default=None, description="Search title/content/error (ILIKE)"),
    success: bool | None = Query(default=None, description="Filter by success"),
    from_date: str | None = Query(default=None, description="Start date (YYYY-MM-DD)"),
    to_date: str | None = Query(default=None, description="End date (YYYY-MM-DD)"),
    db=Depends(get_db),
) -> StreamingResponse:
    """Export filtered notifications as CSV or JSON (no pagination)."""
    normalized = severity.strip().upper() if severity else None
    try:
        rows = NotificationLogService(db).export_logs(
            severity=normalized,
            q=q,
            success=success,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Invalid date: {exc}") from exc

    if format.lower() == "json":
        data = [row.model_dump(mode="json") for row in rows]
        import json

        content = json.dumps(data, ensure_ascii=False, indent=2)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers={
                "Content-Disposition": f"attachment; filename=notifications_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.json"
            },
        )

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["id", "created_at", "severity", "success", "title", "content", "error"])
    for row in rows:
        writer.writerow([
            row.id,
            row.created_at,
            row.severity,
            "true" if row.success else "false",
            row.title,
            row.content,
            row.error,
        ])
    csv_bytes = output.getvalue().encode("utf-8")
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f"attachment; filename=notifications_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.csv"
        },
    )
