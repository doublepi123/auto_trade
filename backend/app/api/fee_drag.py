"""Fee drag analysis API (GET /api/fee-drag/*).

Read-only fee erosion analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.fee_drag_service import FeeDragService

router = APIRouter(
    prefix="/api/fee-drag",
    tags=["fee-drag"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def fee_drag_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize total fees and their drag on gross profits."""
    return FeeDragService(db).summary(days=days)
