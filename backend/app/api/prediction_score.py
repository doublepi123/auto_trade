"""Retrospective conditional-frequency API (GET /api/prediction-score/*).

Read-only historical conditional win rates. Not a live decision signal and
never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.prediction_score_service import PredictionScoreService

router = APIRouter(
    prefix="/api/prediction-score",
    tags=["prediction-score"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/analyze")
def analyze_prediction(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=7, le=3650),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Summarize historical win rates by entry-observable features."""
    return PredictionScoreService(db).analyze(symbol=symbol, lookback_days=lookback_days)
