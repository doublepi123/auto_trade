"""Symbol net-return momentum ranking API (GET /api/momentum-ranking/*).

Read-only cross-sectional momentum ranking.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.momentum_ranking_service import MomentumRankingService

router = APIRouter(
    prefix="/api/momentum-ranking",
    tags=["momentum-ranking"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/rank")
def rank_momentum(
    lookback_days: int = Query(default=90, ge=7, le=3650),
    min_trades: int = Query(default=3, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Rank symbols by cumulative per-trade net-return slope."""
    return MomentumRankingService(db).rank(lookback_days=lookback_days, min_trades=min_trades)
