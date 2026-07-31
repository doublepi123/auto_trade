"""Loss containment API (GET /api/loss-containment/*).

Read-only losing-trade analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.loss_containment_service import LossContainmentService

router = APIRouter(
    prefix="/api/loss-containment",
    tags=["loss-containment"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def loss_containment_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize loss distribution, tail breaches, and exit causes."""
    return LossContainmentService(db).summary(days=days)
