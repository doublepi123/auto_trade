from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.domain.performance.performance_tracker import PerformanceTracker

router = APIRouter(prefix="/api/performance", tags=["performance"])
logger = logging.getLogger("auto_trade.performance")


@router.get("/stats")
def get_stats(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    tracker = PerformanceTracker(db)
    return tracker.get_overall_stats(experiment)


@router.get("/compare")
def compare_variants(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    tracker = PerformanceTracker(db)
    return tracker.compare_variants(experiment)


@router.get("/recommendations")
def get_recommendations(
    experiment: str = Query(..., description="Experiment name"),
    db: Session = Depends(get_db),
) -> list[str]:
    tracker = PerformanceTracker(db)
    return tracker.get_recommendations(experiment)
