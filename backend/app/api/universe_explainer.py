from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.services.universe_explainer_service import UniverseExplainerService


router = APIRouter(
    prefix="/api/universe-explainer",
    tags=["universe-explainer"],
    dependencies=[Depends(require_api_key())],
)


# ----------------------------------------------------------------------
# Response schemas (kept local to avoid schemas.py merge conflicts)
# ----------------------------------------------------------------------
class CandidateSummaryOut(BaseModel):
    symbol: str
    selected: bool
    rank: int | None = None
    score: float
    is_focus: bool = False
    exclusion_reasons: list[str] = Field(default_factory=list)


class ExplainSelectionOut(BaseModel):
    symbol: str
    run_id: int | None = None
    as_of_date: str | None = None
    selected: bool
    rank: int | None = None
    score: float = 0.0
    score_breakdown: dict[str, Any] = Field(default_factory=dict)
    hard_filters_passed: list[str] = Field(default_factory=list)
    hard_filters_failed: list[str] = Field(default_factory=list)
    peer_comparison: list[CandidateSummaryOut] = Field(default_factory=list)


class ExplainRunOut(BaseModel):
    run_id: int | None = None
    as_of_date: str | None = None
    status: str
    total_candidates: int = 0
    selected_count: int = 0
    coverage_ratio: float = 0.0
    top_selected: list[CandidateSummaryOut] = Field(default_factory=list)
    top_rejected: list[CandidateSummaryOut] = Field(default_factory=list)


# ----------------------------------------------------------------------
# Routes (sync handlers, per repo convention)
# ----------------------------------------------------------------------
@router.get("/symbol/{symbol}", response_model=ExplainSelectionOut)
def explain_selection(
    symbol: str,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return UniverseExplainerService(db).explain_selection(symbol)


@router.get("/run", response_model=ExplainRunOut)
def explain_run(
    run_id: int | None = Query(
        default=None,
        description="Selection run id (empty → latest run)",
        ge=1,
    ),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = UniverseExplainerService(db).explain_run(run_id)
    # If the caller asked for a specific run that does not exist, 404 is more
    # useful than a NOT_FOUND-shaped body. An omitted run_id with no runs at
    # all still returns the empty-state body (200).
    if run_id is not None and result["status"] == "NOT_FOUND":
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return result
