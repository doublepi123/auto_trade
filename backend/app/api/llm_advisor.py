from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Optional, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.runner import get_runner
from app.schemas import (
    LLMAnalyzeRequest,
    LLMAnalyzeResponse,
    LLMBudgetStatus,
    LLMInteractionResponse,
    LLMIntervalStatus,
    LLMPreviewAnalyzeRequest,
    LLMSuggestion,
    LLMSymbolStatus,
    MessageResponse,
)
from app.services.trade_execution_service import _PendingOrder
from app.config import settings
from app.services.llm_advisor_service import LLMAdvisorService, build_recent_analysis_context
from app.services.llm_interaction_service import (
    LLMInteractionService,
    build_order_policy_outcome,
)
from app.services.interval_application_service import IntervalApplicationService
from app.services.strategy_service import StrategyService
from app.services.llm_symbol_state_service import LLMSymbolStateService
from app.services.trade_event_service import record_trade_event

logger = logging.getLogger("auto_trade.llm_api")

router = APIRouter(prefix="/api", tags=["llm"])


def _coerce_recent_prices(source: Any) -> list[dict[str, Any]]:
    if not callable(source):
        return []
    try:
        result = source()
    except Exception:
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


def _position_context(symbol: str, current_price: float) -> dict[str, float | str]:
    runner = get_runner()
    try:
        positions = runner.broker.get_positions()
    except Exception:
        logger.exception("failed to load position context for LLM analysis")
        runtime_for_symbol = getattr(runner, "_runtime_for_symbol", None)
        if callable(runtime_for_symbol):
            runtime = runtime_for_symbol(symbol)
            fallback_side = runtime[2].state.value.upper() if isinstance(runtime, tuple) and len(runtime) >= 3 else "FLAT"
        else:
            fallback_side = runner.engine.state.value.upper()
        return {
            "side": fallback_side,
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

    trade_svc = getattr(runner, "_trade_svc", None)
    pending: _PendingOrder | None = None
    if trade_svc is not None:
        pending_for_symbol = getattr(trade_svc, "pending_order_for", None)
        if callable(pending_for_symbol):
            pending = cast(Optional[_PendingOrder], pending_for_symbol(symbol))
        else:
            pending = cast(Optional[_PendingOrder], getattr(trade_svc, "pending_order", None))
    if pending is not None:
        context["pending_order"] = {
            "broker_order_id": pending.broker_order_id,
            "side": pending.action,
            "price": float(pending.price),
            "quantity": float(pending.quantity),
        }
    return context


def _interval_reference_quantity(
    position_context: dict[str, Any],
    account_context: dict[str, Any],
    *,
    current_price: float = 0.0,
    trade_service: Any = None,
) -> float:
    position_quantity = float(position_context.get("quantity") or 0.0)
    if position_quantity > 0:
        return position_quantity
    max_buy_quantity = float(account_context.get("max_buy_quantity") or 0.0)
    if not math.isfinite(max_buy_quantity) or max_buy_quantity <= 0:
        return 1.0

    factor = getattr(trade_service, "margin_safety_factor", None)
    if factor is None:
        factor = 0.9
    try:
        candidate = max_buy_quantity * float(factor)
    except (TypeError, ValueError):
        candidate = 0.0
    if not math.isfinite(candidate) or candidate <= 0:
        return 1.0

    quantity_cap = getattr(trade_service, "max_position_quantity", None)
    if isinstance(quantity_cap, int) and quantity_cap > 0:
        candidate = min(candidate, float(quantity_cap))

    if math.isfinite(current_price) and current_price > 0:
        notional_cap = getattr(trade_service, "max_position_notional", None)
        if isinstance(notional_cap, (int, float)) and math.isfinite(float(notional_cap)) and float(notional_cap) > 0:
            candidate = min(candidate, float(notional_cap) / current_price)
        risk_cap = getattr(trade_service, "max_risk_per_trade", None)
        stop_loss_pct = getattr(trade_service, "stop_loss_pct", None)
        if (
            isinstance(risk_cap, (int, float))
            and isinstance(stop_loss_pct, (int, float))
            and math.isfinite(float(risk_cap))
            and math.isfinite(float(stop_loss_pct))
            and float(risk_cap) > 0
            and float(stop_loss_pct) > 0
        ):
            stop_distance = current_price * float(stop_loss_pct) / 100
            candidate = min(candidate, float(risk_cap) / stop_distance)

    return max(float(math.floor(candidate)), 1.0)


@router.post("/strategy/llm-interval/preview", response_model=LLMAnalyzeResponse, dependencies=[Depends(require_api_key())])
def preview_llm_interval(payload: LLMPreviewAnalyzeRequest) -> LLMAnalyzeResponse:
    try:
        runner = get_runner()
    except Exception:
        raise HTTPException(status_code=503, detail="runner not initialized") from None
    advisor = LLMAdvisorService(broker=runner.broker)
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


@router.post("/strategy/llm-interval/analyze", response_model=LLMAnalyzeResponse, dependencies=[Depends(require_api_key())])
def analyze_llm_interval(
    payload: LLMAnalyzeRequest,
    db: Session = Depends(get_db),
) -> LLMAnalyzeResponse:
    svc = StrategyService(db)
    config = svc.get_config()

    if not config.symbol:
        raise HTTPException(status_code=400, detail="Strategy symbol not configured")

    try:
        runner = get_runner()
    except Exception:
        raise HTTPException(status_code=503, detail="runner not initialized") from None
    current_price = runner.fresh_market_price(config.symbol)
    if current_price is None or not math.isfinite(current_price) or current_price <= 0:
        raise HTTPException(status_code=400, detail="current price unavailable")
    position_context = _position_context(config.symbol, current_price)
    recent_price_context = getattr(runner, "recent_price_context", None)
    account_context = _account_context(config.symbol, config.market, current_price, config.short_selling)
    advisor = LLMAdvisorService(broker=runner.broker)
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
        recent_prices=_coerce_recent_prices(recent_price_context),
        recent_analysis=build_recent_analysis_context(config),
        account_context=account_context,
        force=payload.force,
    )

    if not result["success"]:
        record_trade_event(
            db,
            event_type="LLM_ANALYSIS",
            symbol=config.symbol,
            status="FAILED",
            message=result.get("error", "Unknown error"),
            payload={"interaction_id": result.get("interaction_id")},
        )
        db.commit()
        return LLMAnalyzeResponse(
            success=False,
            applied=False,
            reason=result.get("error", "Unknown error"),
            interaction_id=result.get("interaction_id"),
        )

    from app.api.strategy import _reload_strategy_after_save

    app_svc = IntervalApplicationService()
    app_result = app_svc.apply_suggestion(
        db=db,
        engine_state=runner.engine.state.value.lower(),
        current_price=current_price,
        suggestion={
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "confidence_score": result.get("confidence_score"),
        },
        reference_quantity=_interval_reference_quantity(
            position_context,
            account_context,
            current_price=current_price,
            trade_service=getattr(runner, "_trade_svc", None),
        ),
        runtime_reload=_reload_strategy_after_save,
    )
    order_result = {"executed": False, "status": "NO_ACTION", "order_id": None}
    if result.get("order_action") and result.get("order_action") != "NONE":
        try:
            order_result = runner.execute_llm_order_decision(result)
        except Exception:
            logger.exception("failed to execute LLM order action")
            order_result = {"executed": False, "status": "ERROR", "order_id": None}
    policy_outcome = build_order_policy_outcome(result, order_result)

    interaction_id = result.get("interaction_id")
    if interaction_id is not None:
        try:
            LLMInteractionService(db).update_outcome(
                interaction_id,
                applied=app_result["applied"],
                order_status=cast(Optional[str], order_result.get("status")),
                order_id=cast(Optional[str], order_result.get("order_id")),
                policy_outcome=policy_outcome,
            )
        except Exception:
            logger.exception("failed to update LLM interaction outcome")

    record_trade_event(
        db,
        event_type="LLM_ANALYSIS",
        symbol=config.symbol,
        status="SUCCESS",
        message=result.get("analysis") or app_result["reason"],
        payload={
            "interaction_id": interaction_id,
            "confidence_score": result.get("confidence_score"),
            "suggested_buy_low": result.get("suggested_buy_low"),
            "suggested_sell_high": result.get("suggested_sell_high"),
            "applied": app_result["applied"],
            "apply_reason": app_result["reason"],
            "order_action": result.get("order_action"),
            "order_status": order_result.get("status"),
            "order_id": order_result.get("order_id"),
            "order_reject_reason": order_result.get("reason"),
            "policy_outcome": policy_outcome,
        },
    )
    db.commit()
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
        order_status=cast(Optional[str], order_result.get("status")),
        order_id=cast(Optional[str], order_result.get("order_id")),
    )


@router.get(
    "/strategy/llm-interval/interactions",
    response_model=list[LLMInteractionResponse],
    dependencies=[Depends(require_api_key())],
)
def get_llm_interactions(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[LLMInteractionResponse]:
    records = LLMInteractionService(db).list_recent(limit)
    return [LLMInteractionResponse.model_validate(record) for record in records]


@router.get(
    "/strategy/llm-interval/status",
    response_model=LLMIntervalStatus,
    dependencies=[Depends(require_api_key())],
)
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

    last_applied_values = None
    if config.llm_applied_buy_low is not None and config.llm_applied_sell_high is not None:
        last_applied_values = {
            "buy_low": config.llm_applied_buy_low,
            "sell_high": config.llm_applied_sell_high,
        }
    applied_values = None if settings.llm_shadow_mode else last_applied_values

    next_analysis_at = config.llm_next_analysis_at
    if config.llm_last_analysis_at is not None:
        last_analysis_at = config.llm_last_analysis_at
        if last_analysis_at.tzinfo is None:
            last_analysis_at = last_analysis_at.replace(tzinfo=timezone.utc)
        next_analysis_at = last_analysis_at + timedelta(minutes=config.llm_interval_minutes)

    state_svc = LLMSymbolStateService(db)
    persisted_states = state_svc.states_by_symbol()
    raw_symbol_statuses = get_runner().llm_symbol_statuses()
    symbol_statuses: list[LLMSymbolStatus] = []
    for item in raw_symbol_statuses:
        state = persisted_states.get(item["symbol"])
        symbol_statuses.append(
            LLMSymbolStatus(
                **item,
                last_analysis_at=state.last_analysis_at.isoformat() if state and state.last_analysis_at else None,
                next_analysis_at=state.next_analysis_at.isoformat() if state and state.next_analysis_at else None,
                last_status=state.last_status if state and state.last_status else None,
                last_skip_reason=state.last_skip_reason if state and state.last_skip_reason else None,
            )
        )
    tracked_symbol_count = len(symbol_statuses)
    used_analyses_last_hour = state_svc.count_analyses_last_hour(datetime.now(timezone.utc))
    remaining_analyses_this_hour = max(0, settings.llm_max_analyses_per_hour - used_analyses_last_hour)

    return LLMIntervalStatus(
        enabled=config.auto_interval_enabled,
        shadow_mode=settings.llm_shadow_mode,
        policy_status="SHADOW" if settings.llm_shadow_mode else "LIVE",
        interval_minutes=config.llm_interval_minutes,
        last_analysis_at=config.llm_last_analysis_at.isoformat() if config.llm_last_analysis_at else None,
        next_analysis_at=next_analysis_at.isoformat() if next_analysis_at else None,
        current_suggestion=current_suggestion,
        applied_values=applied_values,
        last_applied_values=last_applied_values,
        reject_reason=config.llm_reject_reason,
        budget=LLMBudgetStatus(
            max_symbols_per_cycle=settings.llm_max_symbols_per_cycle,
            max_analyses_per_hour=settings.llm_max_analyses_per_hour,
            tracked_symbol_count=tracked_symbol_count,
            effective_symbol_budget=min(
                settings.llm_max_symbols_per_cycle,
                tracked_symbol_count,
                remaining_analyses_this_hour,
            ),
            used_analyses_last_hour=used_analyses_last_hour,
            remaining_analyses_this_hour=remaining_analyses_this_hour,
        ),
        symbol_statuses=symbol_statuses,
    )


@router.put("/strategy/llm-interval/enable", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def enable_llm_interval(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = True
    db.commit()
    db.refresh(config)
    reload_warning = None
    try:
        from app.api.strategy import _reload_strategy_after_save
        _reload_strategy_after_save()
    except Exception:
        logger.exception("strategy reload failed after enabling LLM interval")
        reload_warning = (
            "LLM interval enabled but live reload failed. "
            "A restart may be required for changes to take effect."
        )
    actor_hash, source_ip = extract_actor(request)
    audit.record(
        "LLM_INTERVAL_ENABLE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"action": "enable"},
        result="SUCCESS",
    )
    msg = "LLM auto interval enabled"
    if reload_warning:
        msg = f"{msg}. {reload_warning}"
    return MessageResponse(message=msg)


@router.put("/strategy/llm-interval/disable", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def disable_llm_interval(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    config.auto_interval_enabled = False
    db.commit()
    db.refresh(config)
    reload_warning = None
    try:
        from app.api.strategy import _reload_strategy_after_save
        _reload_strategy_after_save()
    except Exception:
        logger.exception("strategy reload failed after disabling LLM interval")
        reload_warning = (
            "LLM interval disabled but live reload failed. "
            "A restart may be required for changes to take effect."
        )
    actor_hash, source_ip = extract_actor(request)
    audit.record(
        "LLM_INTERVAL_DISABLE",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"action": "disable"},
        result="SUCCESS",
    )
    msg = "LLM auto interval disabled"
    if reload_warning:
        msg = f"{msg}. {reload_warning}"
    return MessageResponse(message=msg)
