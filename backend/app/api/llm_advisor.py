from __future__ import annotations

import logging
from datetime import timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.runner import get_runner
from app.schemas import LLMAnalyzeRequest, LLMAnalyzeResponse, LLMInteractionResponse, LLMIntervalStatus, LLMPreviewAnalyzeRequest, LLMSuggestion, MessageResponse
from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
from app.services.llm_interaction_service import LLMInteractionService
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService

logger = logging.getLogger("auto_trade.llm_api")

router = APIRouter(prefix="/api", tags=["llm"])


def _position_context(symbol: str, current_price: float) -> dict[str, float | str]:
    runner = get_runner()
    try:
        positions = runner.broker.get_positions()
    except Exception:
        logger.exception("failed to load position context for LLM analysis")
        return {
            "side": runner.engine.state.value.upper(),
            "quantity": 0.0,
            "avg_price": 0.0,
            "unrealized_pnl_pct": 0.0,
        }

    position = next((p for p in positions if p.symbol == symbol and p.quantity > 0), None)
    if position is None:
        return {"side": "FLAT", "quantity": 0.0, "avg_price": 0.0, "unrealized_pnl_pct": 0.0}

    avg_price = float(position.avg_price)
    if avg_price <= 0:
        pnl_pct = 0.0
    elif position.side == "SHORT":
        pnl_pct = (avg_price - current_price) / avg_price * 100
    else:
        pnl_pct = (current_price - avg_price) / avg_price * 100

    return {
        "side": position.side,
        "quantity": float(position.quantity),
        "avg_price": avg_price,
        "unrealized_pnl_pct": pnl_pct,
    }


def _cash_currency(market: str) -> str:
    return "HKD" if market == "HK" else "USD"


def _account_context(symbol: str, market: str, current_price: float, short_selling: bool) -> dict[str, Any]:
    runner = get_runner()
    broker = getattr(runner, "broker", None)
    currency = _cash_currency(market)
    context: dict[str, Any] = {
        "cash_currency": currency,
        "available_cash": 0.0,
        "buying_power": 0.0,
        "max_buy_quantity": 0.0,
        "max_short_quantity": 0.0,
        "pending_order": None,
        "errors": [],
    }
    if broker is None:
        context["errors"].append("broker unavailable")
        return context
    try:
        available_cash = broker.get_cash(currency)
        context["available_cash"] = float(available_cash)
    except Exception as exc:
        logger.warning("failed to load buying power cash context for LLM analysis: %s", exc)
        context["errors"].append(f"cash unavailable: {exc}")

    price = Decimal(str(current_price)) if current_price > 0 else Decimal("0")
    if price > 0:
        try:
            max_buy = broker.estimate_margin_max_quantity(symbol, "BUY", price, currency)
            context["max_buy_quantity"] = float(max_buy)
            context["buying_power"] = float(max_buy * price)
        except Exception as exc:
            logger.warning("failed to estimate buy quantity for LLM analysis: %s", exc)
            context["errors"].append(f"buy quantity unavailable: {exc}")
        if short_selling:
            try:
                max_short = broker.estimate_margin_max_quantity(symbol, "SELL", price, currency)
                context["max_short_quantity"] = float(max_short)
            except Exception as exc:
                logger.warning("failed to estimate short quantity for LLM analysis: %s", exc)
                context["errors"].append(f"short quantity unavailable: {exc}")

    pending = getattr(getattr(runner, "_trade_svc", None), "_pending_order", None)
    if pending is not None:
        context["pending_order"] = {
            "broker_order_id": pending.broker_order_id,
            "side": pending.action,
            "price": float(pending.price),
            "quantity": float(pending.quantity),
        }
    return context


@router.post("/strategy/llm-interval/preview", response_model=LLMAnalyzeResponse, dependencies=[Depends(require_api_key())])
def preview_llm_interval(payload: LLMPreviewAnalyzeRequest) -> LLMAnalyzeResponse:
    advisor = LLMAdvisorService()
    result = advisor.preview(
        symbol=payload.symbol,
        market=payload.market,
        current_price=payload.current_price or 0.0,
        current_buy_low=payload.current_buy_low or 0.0,
        current_sell_high=payload.current_sell_high or 0.0,
        short_selling=payload.short_selling,
        min_profit_amount=payload.min_profit_amount or 0.0,
        account_context=_account_context(
            payload.symbol,
            payload.market,
            payload.current_price or 0.0,
            payload.short_selling,
        ),
    )
    if not result["success"]:
        return LLMAnalyzeResponse(success=False, applied=False, reason=result.get("error", "Unknown error"))
    return LLMAnalyzeResponse(
        success=True,
        applied=False,
        reason=result["reason"],
        interaction_id=result.get("interaction_id"),
        suggested_buy_low=result.get("suggested_buy_low"),
        suggested_sell_high=result.get("suggested_sell_high"),
        confidence_score=result.get("confidence_score"),
        analysis=result.get("analysis"),
        next_analysis_at=None,
        applied_at=None,
        order_action=result.get("order_action"),
    )


@router.post("/strategy/llm-interval/analyze", response_model=LLMAnalyzeResponse)
def analyze_llm_interval(
    payload: LLMAnalyzeRequest,
    db: Session = Depends(get_db),
) -> LLMAnalyzeResponse:
    svc = StrategyService(db)
    config = svc.get_config()

    if not config.symbol:
        raise HTTPException(status_code=400, detail="Strategy symbol not configured")

    runner = get_runner()
    last_price = runner.engine.last_price
    current_price = last_price if last_price else config.buy_low
    position_context = _position_context(config.symbol, current_price)
    recent_price_context = getattr(runner, "recent_price_context", None)
    advisor = LLMAdvisorService()
    result = advisor.analyze(
        symbol=config.symbol,
        market=config.market,
        current_price=current_price,
        current_buy_low=config.buy_low,
        current_sell_high=config.sell_high,
        short_selling=config.short_selling,
        current_position=str(position_context["side"]),
        recent_trades=[],
        position_quantity=float(position_context["quantity"]),
        position_avg_price=float(position_context["avg_price"]),
        unrealized_pnl_pct=float(position_context["unrealized_pnl_pct"]),
        min_profit_amount=config.min_profit_amount,
        recent_prices=recent_price_context() if callable(recent_price_context) else [],
        recent_analysis=build_recent_analysis_context(config),
        account_context=_account_context(config.symbol, config.market, current_price, config.short_selling),
        force=payload.force,
    )

    if not result["success"]:
        return LLMAnalyzeResponse(
            success=False,
            applied=False,
            reason=result.get("error", "Unknown error"),
            interaction_id=result.get("interaction_id"),
        )

    app_svc = IntervalApplicationService()
    app_result = app_svc.apply_direct_suggestion(
        db=db,
        current_price=current_price,
        suggestion={
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
        },
    )
    order_result = {"executed": False, "status": "NO_ACTION", "order_id": None}
    if result.get("order_action") and result.get("order_action") != "NONE":
        try:
            order_result = runner.execute_llm_order_decision(result)
        except Exception:
            logger.exception("failed to execute LLM order action")
            order_result = {"executed": False, "status": "ERROR", "order_id": None}

    interaction_id = result.get("interaction_id")
    if interaction_id is not None:
        try:
            LLMInteractionService(db).update_outcome(
                interaction_id,
                applied=app_result["applied"],
                order_status=order_result.get("status"),
                order_id=order_result.get("order_id"),
            )
        except Exception:
            logger.exception("failed to update LLM interaction outcome")

    return LLMAnalyzeResponse(
        success=True,
        applied=app_result["applied"],
        reason=app_result["reason"],
        interaction_id=interaction_id,
        suggested_buy_low=result.get("suggested_buy_low"),
        suggested_sell_high=result.get("suggested_sell_high"),
        confidence_score=result.get("confidence_score"),
        analysis=result.get("analysis"),
        next_analysis_at=result.get("next_analysis_at"),
        applied_at=app_result.get("applied_at") if app_result["applied"] else None,
        order_action=result.get("order_action"),
        order_price=result.get("order_price"),
        replacement_action=result.get("replacement_action"),
        replacement_price=result.get("replacement_price"),
        order_reason=result.get("order_reason"),
        order_status=order_result.get("status"),
        order_id=order_result.get("order_id"),
    )


@router.get("/strategy/llm-interval/interactions", response_model=list[LLMInteractionResponse])
def get_llm_interactions(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[LLMInteractionResponse]:
    records = LLMInteractionService(db).list_recent(limit)
    return [LLMInteractionResponse.model_validate(record) for record in records]


@router.get("/strategy/llm-interval/status", response_model=LLMIntervalStatus)
def get_llm_interval_status(db: Session = Depends(get_db)) -> LLMIntervalStatus:
    svc = StrategyService(db)
    config = svc.get_config()

    current_suggestion: LLMSuggestion | None = None
    if config.llm_suggested_buy_low is not None and config.llm_suggested_sell_high is not None:
        current_suggestion = LLMSuggestion(
            buy_low=config.llm_suggested_buy_low,
            sell_high=config.llm_suggested_sell_high,
            confidence_score=config.llm_confidence_score or 0.0,
            analysis=config.llm_analysis or "",
        )

    applied_values = None
    if config.llm_applied_buy_low is not None and config.llm_applied_sell_high is not None:
        applied_values = {
            "buy_low": config.llm_applied_buy_low,
            "sell_high": config.llm_applied_sell_high,
        }

    next_analysis_at = config.llm_next_analysis_at
    if config.llm_last_analysis_at is not None:
        last_analysis_at = config.llm_last_analysis_at
        if last_analysis_at.tzinfo is None:
            last_analysis_at = last_analysis_at.replace(tzinfo=timezone.utc)
        next_analysis_at = last_analysis_at + timedelta(minutes=config.llm_interval_minutes)

    return LLMIntervalStatus(
        enabled=config.auto_interval_enabled,
        interval_minutes=config.llm_interval_minutes,
        last_analysis_at=config.llm_last_analysis_at.isoformat() if config.llm_last_analysis_at else None,
        next_analysis_at=next_analysis_at.isoformat() if next_analysis_at else None,
        current_suggestion=current_suggestion,
        applied_values=applied_values,
        reject_reason=config.llm_reject_reason,
    )


@router.put("/strategy/llm-interval/enable", response_model=MessageResponse)
def enable_llm_interval(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = True
    db.commit()
    return MessageResponse(message="LLM auto interval enabled")


@router.put("/strategy/llm-interval/disable", response_model=MessageResponse)
def disable_llm_interval(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = False
    db.commit()
    return MessageResponse(message="LLM auto interval disabled")
