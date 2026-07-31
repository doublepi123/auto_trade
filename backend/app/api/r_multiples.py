"""Realized-loss proxy distribution API (GET /api/r-multiples/*).

Read-only post-hoc outcome analytics. This is not true initial-risk R and
never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.r_multiples_service import RMultiplesService

router = APIRouter(
    prefix="/api/r-multiples",
    tags=["r-multiples"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/distribution")
def r_multiples_distribution(
    days: int = Query(default=90, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Distribute outcomes in units of the sample's mean realized loss."""
    return RMultiplesService(db).distribution(days=days)
