from __future__ import annotations

from fastapi import APIRouter, Depends

from app.api.auth import require_api_key
from app.database import get_db
from app.runner import get_runner
from app.schemas import PositionPnlResult
from app.services.position_pnl_service import PositionPnlService

router = APIRouter(
    prefix="/api/positions",
    tags=["positions"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/pnl", response_model=PositionPnlResult)
def get_positions_pnl(db=Depends(get_db)) -> PositionPnlResult:
    """Live unrealized P&L over open positions: tracked_entries cost joined to
    live broker quotes. Read-only."""
    runner = get_runner()
    broker = getattr(runner, "broker", None)
    return PositionPnlService(db, broker).get_positions_pnl()
