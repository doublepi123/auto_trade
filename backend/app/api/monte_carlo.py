"""Monte Carlo simulation API (GET /api/monte-carlo/*).

Read-only bootstrap resampling of trade PnLs.  Never writes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.monte_carlo_service import MonteCarloService

router = APIRouter(
    prefix="/api/monte-carlo",
    tags=["monte-carlo"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/simulate")
def simulate(
    symbol: str | None = Query(default=None),
    lookback_days: int = Query(default=180, ge=1, le=3650),
    n_simulations: int = Query(default=1000, ge=10, le=50000),
    n_trades: int | None = Query(default=None, ge=1, le=10000),
    seed: int = Query(default=42),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    """Run Monte Carlo bootstrap simulation on historical trade PnLs."""
    return MonteCarloService(db).simulate(
        symbol=symbol,
        lookback_days=lookback_days,
        n_simulations=n_simulations,
        n_trades=n_trades,
        seed=seed,
    )
