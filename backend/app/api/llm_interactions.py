from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import LLMInteractionDetail
from app.services.llm_interaction_service import LLMInteractionService

router = APIRouter(
    prefix="/api/llm-interactions",
    tags=["llm-interactions"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/{interaction_id}", response_model=LLMInteractionDetail)
def get_llm_interaction(interaction_id: int, db=Depends(get_db)) -> LLMInteractionDetail:
    """Full LLM interaction detail (prompt + raw response + parsed + context)."""
    out = LLMInteractionService(db).get_detail(interaction_id)
    if out is None:
        raise HTTPException(status_code=404, detail="llm interaction not found")
    return out
