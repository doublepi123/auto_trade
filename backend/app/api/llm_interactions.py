from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import LLMInteractionDetail, LLMInteractionPage
from app.services.llm_interaction_service import LLMInteractionService

router = APIRouter(
    prefix="/api/llm-interactions",
    tags=["llm-interactions"],
    dependencies=[Depends(require_api_key())],
)


@router.get("", response_model=LLMInteractionPage)
def list_llm_interactions(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    symbol: str | None = Query(default=None, max_length=50),
    success: bool | None = Query(default=None),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    db=Depends(get_db),
) -> LLMInteractionPage:
    """Paginated, filtered LLM interaction list with a safe projection.

    Never includes prompt, raw_response, parsed_response, or context_snapshot.
    Half-open datetime range ``[from, to)``. Stable newest-first ordering.
    """
    if from_ is not None and to is not None and from_ >= to:
        raise HTTPException(
            status_code=422,
            detail="from must be before to",
        )
    items, total = LLMInteractionService(db).list_interactions(
        page=page,
        page_size=page_size,
        symbol=symbol,
        success=success,
        from_dt=from_,
        to_dt=to,
    )
    return LLMInteractionPage(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/{interaction_id}", response_model=LLMInteractionDetail)
def get_llm_interaction(interaction_id: int, db=Depends(get_db)) -> LLMInteractionDetail:
    """Full LLM interaction detail (prompt + raw response + parsed + context)."""
    out = LLMInteractionService(db).get_detail(interaction_id)
    if out is None:
        raise HTTPException(status_code=404, detail="llm interaction not found")
    return out
