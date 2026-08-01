from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import LLMUsageBySymbolResponse, LLMUsageSummaryResponse
from app.services.llm_usage_service import LLMUsageService


router = APIRouter(
    prefix="/api/llm-usage",
    tags=["llm"],
    dependencies=[Depends(require_api_key())],
)


@router.get("/summary", response_model=LLMUsageSummaryResponse)
def get_llm_usage_summary(
    days: int = Query(default=30, ge=1, le=365),
    db: Session = Depends(get_db),
) -> LLMUsageSummaryResponse:
    return LLMUsageService(db).summary(days)


@router.get("/by-symbol", response_model=LLMUsageBySymbolResponse)
def get_llm_usage_by_symbol(
    days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> LLMUsageBySymbolResponse:
    """LLM usage aggregated by symbol (and market). Read-only, authenticated."""
    return LLMUsageService(db).by_symbol(days, limit)
