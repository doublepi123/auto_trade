"""Exit efficiency API (GET /api/exit-efficiency/*).

Read-only MFE/MAE exit analytics.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.exit_efficiency_service import ExitEfficiencyService

router = APIRouter(
    prefix="/api/exit-efficiency",
    tags=["exit-efficiency"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary")
def exit_efficiency_summary(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize MFE capture rate, giveback, and MAE tolerance."""
    return ExitEfficiencyService(db).summary(days=days)
