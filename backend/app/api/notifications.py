from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import NotificationLogPage
from app.services.notification_log_service import NotificationLogService

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
