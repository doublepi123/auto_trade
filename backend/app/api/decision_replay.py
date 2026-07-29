from __future__ import annotations

"""Trade decision replay (GET /api/decision-replay/*). Read-only.

* ``GET /trades`` — recent closed round trips (most-recent first) with the
  number of related trade events, ready to populate a replay picker.
* ``GET /trade/{trade_id}`` — full chronological timeline of decision points
  (price updates, LLM analysis, risk gates, order submission/fill, skips and
  the final close) for one closed round trip.

A trade is addressed by the id of either of its two order-ledger rows
(entry or exit).
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.decision_replay_service import DecisionReplayService

router = APIRouter(
    prefix="/api/decision-replay",
    tags=["decision-replay"],
    dependencies=[Depends(require_api_key())],
)


class TimelineEntry(BaseModel):
    """One decision point on the replay timeline."""

    timestamp: str = Field(..., description="ISO timestamp of the event")
    event_type: str = Field(..., description="TradeEvent.event_type")
    status: str = Field(..., description="TradeEvent.status (may be empty)")
    message: str = Field(..., description="TradeEvent.message (may be empty)")
    payload_summary: dict[str, object] = Field(
        default_factory=dict,
        description="Truncated, JSON-safe view of the event payload",
    )


class TradeReplay(BaseModel):
    """Full replay of one closed round trip."""

    trade_id: int
    symbol: str
    market: str = Field(..., description="Inferred from symbol suffix (.US / .HK)")
    side: str
    entry_price: float
    exit_price: float
    pnl: float
    entry_time: str
    exit_time: str
    timeline: list[TimelineEntry]


class ReplayableTrade(BaseModel):
    """One closed round trip available for replay."""

    trade_id: int = Field(..., description="Exit order id (the canonical trade id)")
    entry_order_id: int
    exit_order_id: int
    symbol: str
    market: str
    side: str
    pnl: float
    entry_time: str
    exit_time: str
    event_count: int = Field(..., description="Related TradeEvents in the window")


@router.get("/trade/{trade_id}", response_model=TradeReplay)
def replay_trade(
    trade_id: int,
    db: Session = Depends(get_db),
) -> TradeReplay:
    """Full chronological replay of one closed round trip.

    ``trade_id`` may be the entry OR exit order id of the round trip. Returns
    404 when no closed round trip matches.
    """
    replay = DecisionReplayService(db).replay_trade(trade_id)
    if replay is None:
        raise HTTPException(status_code=404, detail="trade not found")
    return TradeReplay.model_validate(replay)


@router.get("/trades", response_model=list[ReplayableTrade])
def list_replayable_trades(
    limit: int = Query(default=50, ge=1, le=500, description="Max trades returned"),
    symbol: str | None = Query(
        default=None,
        description="Restrict to one symbol (e.g. AAPL.US). Default: all symbols.",
    ),
    db: Session = Depends(get_db),
) -> list[ReplayableTrade]:
    """Recent closed round trips (most-recent exit first) with event counts."""
    rows = DecisionReplayService(db).list_replayable_trades(limit=limit, symbol=symbol)
    return [ReplayableTrade.model_validate(row) for row in rows]
