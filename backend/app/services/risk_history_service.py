"""Daily risk history — reads runtime_state_snapshots to show risk over time."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import RuntimeStateSnapshot
from app.schemas import RiskHistoryPoint, RiskHistoryResponse


class RiskHistoryService:
    def __init__(self, db: Session) -> None:
        self._db = db

    def get_history(self, *, symbol: str | None = None, limit: int = 100) -> RiskHistoryResponse:
        capped = max(1, min(limit, 500))
        stmt = select(RuntimeStateSnapshot)
        if symbol:
            stmt = stmt.where(RuntimeStateSnapshot.symbol == symbol)
        stmt = stmt.order_by(RuntimeStateSnapshot.created_at.desc()).limit(capped)
        rows = list(self._db.scalars(stmt))
        rows = list(reversed(rows))  # chronological for charting
        points = [
            RiskHistoryPoint(
                created_at=r.created_at,
                engine_state=r.engine_state,
                paused=bool(r.paused),
                kill_switch=bool(r.kill_switch),
                daily_pnl=float(r.daily_pnl),
                consecutive_losses=int(r.consecutive_losses),
            )
            for r in rows
        ]
        return RiskHistoryResponse(points=points, latest=points[-1] if points else None)
