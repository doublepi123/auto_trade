from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.runner import get_runner
from app.schemas import LLMAnalyzeRequest, LLMAnalyzeResponse, LLMIntervalStatus, MessageResponse
from app.services.llm_advisor_service import LLMAdvisorService
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.llm_api")

router = APIRouter(prefix="/api", tags=["llm"])


@router.post("/strategy/llm-interval/analyze", response_model=LLMAnalyzeResponse, dependencies=[Depends(require_api_key())])
def analyze_llm_interval(
    payload: LLMAnalyzeRequest,
    db: Session = Depends(get_db),
) -> LLMAnalyzeResponse:
    svc = StrategyService(db)
    config = svc.get_config()

    if not config.symbol:
        raise HTTPException(status_code=400, detail="Strategy symbol not configured")

    advisor = LLMAdvisorService()
    result = advisor.analyze(
        symbol=config.symbol,
        market=config.market,
        current_price=config.buy_low,
        current_buy_low=config.buy_low,
        current_sell_high=config.sell_high,
        short_selling=config.short_selling,
        current_position=get_runner().engine.state.value,
        recent_trades=[],
        force=payload.force,
    )

    if not result["success"]:
        return LLMAnalyzeResponse(
            success=False,
            reason=result.get("error", "Unknown error"),
        )

    if config.auto_interval_enabled:
        app_svc = IntervalApplicationService()
        app_result = app_svc.apply_suggestion(
            db=db,
            engine_state=get_runner().engine.state.value,
            current_price=get_runner().engine.last_price or config.buy_low,
            current_buy_low=config.buy_low,
            current_sell_high=config.sell_high,
            suggestion={
                "suggested_buy_low": result.get("suggested_buy_low"),
                "suggested_sell_high": result.get("suggested_sell_high"),
                "confidence_score": result.get("confidence_score"),
            },
        )
        return LLMAnalyzeResponse(
            success=True,
            applied=app_result["applied"],
            reason=app_result["reason"],
            suggested_buy_low=result.get("suggested_buy_low"),
            suggested_sell_high=result.get("suggested_sell_high"),
            confidence_score=result.get("confidence_score"),
            analysis=result.get("analysis"),
            next_analysis_at=result.get("next_analysis_at"),
            applied_at=app_result.get("applied_at") if app_result["applied"] else None,
        )

    return LLMAnalyzeResponse(
        success=True,
        applied=False,
        reason="Auto interval not enabled",
        suggested_buy_low=result.get("suggested_buy_low"),
        suggested_sell_high=result.get("suggested_sell_high"),
        confidence_score=result.get("confidence_score"),
        analysis=result.get("analysis"),
        next_analysis_at=result.get("next_analysis_at"),
    )


@router.get("/strategy/llm-interval/status", response_model=LLMIntervalStatus, dependencies=[Depends(require_api_key())])
def get_llm_interval_status(db: Session = Depends(get_db)) -> LLMIntervalStatus:
    svc = StrategyService(db)
    config = svc.get_config()

    current_suggestion = None
    if config.llm_suggested_buy_low is not None and config.llm_suggested_sell_high is not None:
        current_suggestion = {
            "buy_low": config.llm_suggested_buy_low,
            "sell_high": config.llm_suggested_sell_high,
            "confidence_score": config.llm_confidence_score or 0.0,
            "analysis": config.llm_analysis or "",
        }

    applied_values = None
    if config.llm_applied_buy_low is not None and config.llm_applied_sell_high is not None:
        applied_values = {
            "buy_low": config.llm_applied_buy_low,
            "sell_high": config.llm_applied_sell_high,
        }

    return LLMIntervalStatus(
        enabled=config.auto_interval_enabled,
        last_analysis_at=config.llm_last_analysis_at.isoformat() if config.llm_last_analysis_at else None,
        next_analysis_at=config.llm_next_analysis_at.isoformat() if config.llm_next_analysis_at else None,
        current_suggestion=current_suggestion,
        applied_values=applied_values,
        reject_reason=config.llm_reject_reason,
    )


@router.put("/strategy/llm-interval/enable", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def enable_llm_interval(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = True
    db.commit()
    return MessageResponse(message="LLM auto interval enabled")


@router.put("/strategy/llm-interval/disable", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def disable_llm_interval(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = False
    db.commit()
    return MessageResponse(message="LLM auto interval disabled")
