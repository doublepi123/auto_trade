from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.signal_consensus_service import SignalConsensusService


router = APIRouter(
    prefix="/api/signal-consensus",
    tags=["signal-consensus"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# Response schemas (kept local to avoid schemas.py merge conflicts)
# ----------------------------------------------------------------------
class SignalSourceOut(BaseModel):
    signal: str = Field(description="BULLISH | BEARISH | NEUTRAL | NO_DATA")
    confidence: float = Field(ge=0.0, le=1.0)
    detail: str = ""
    updated_at: str | None = None


class ConsensusRowOut(BaseModel):
    symbol: str
    sources: dict[str, SignalSourceOut]
    consensus: str = Field(
        description="AGREE_BULLISH | AGREE_BEARISH | MIXED | INSUFFICIENT_DATA"
    )
    agreement_score: float = Field(ge=0.0, le=1.0)


class ConsensusSummaryOut(BaseModel):
    total_symbols: int
    agree_bullish: int
    agree_bearish: int
    mixed: int
    insufficient: int


# ----------------------------------------------------------------------
# Routes (sync handlers, per repo convention)
# ----------------------------------------------------------------------
def _parse_symbols(raw: str | None) -> list[str] | None:
    if raw is None:
        return None
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts or None


@router.get("/matrix", response_model=list[ConsensusRowOut])
def get_matrix(
    symbols: str | None = Query(
        default=None,
        description="Comma-separated symbol list (empty → union of all sources)",
    ),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    return SignalConsensusService(db).get_matrix(_parse_symbols(symbols))


@router.get("/summary", response_model=ConsensusSummaryOut)
def get_summary(
    symbols: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ConsensusSummaryOut:
    summary = SignalConsensusService(db).get_summary(_parse_symbols(symbols))
    return ConsensusSummaryOut(**summary)
