from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.core.market_calendar import is_trading_hours, trade_day_for
from app.database import get_db
from app.models import OrderRecord
from app.runner import AppRunner, get_runner
from app.schemas import DiagnosticsResponse, StatusHistoryPoint, StatusHistoryResponse, StatusResponse, StrategyConfigSchema, StrategyMergedSchema, StrategyResponse, TradeSignalMarker
from app.services.daily_pnl_service import DailyPnlService
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService, validate_strategy_consistency

logger = logging.getLogger("auto_trade.strategy")

router = APIRouter(prefix="/api", tags=["strategy"])


def _reload_strategy_safely(runner: AppRunner) -> None:
    try:
        runner.reload_strategy()
    except Exception:
        logger.exception("failed to reload strategy into running engine")


def _reload_strategy_after_save() -> None:
    runner = get_runner()
    if runner.is_running:
        threading.Thread(target=_reload_strategy_safely, args=(runner,), daemon=True).start()
        return
    _reload_strategy_safely(runner)


@router.get("/strategy", response_model=StrategyResponse, dependencies=[Depends(require_api_key())])
def get_strategy(db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    return StrategyResponse.model_validate(config)


@router.put("/strategy", dependencies=[Depends(require_api_key())])
def put_strategy(
    request: Request,
    payload: StrategyConfigSchema,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StrategyResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    diff: dict[str, Any] = {}
    try:
        svc = StrategyService(db)
        current = svc.get_config()
        data = payload.model_dump(exclude_unset=True)
        merged = {
            "symbol": data["symbol"] if "symbol" in data and data["symbol"] is not None else current.symbol,
            "market": data["market"] if "market" in data and data["market"] is not None else current.market,
            "buy_low": data["buy_low"] if "buy_low" in data and data["buy_low"] is not None else current.buy_low,
            "sell_high": data["sell_high"] if "sell_high" in data and data["sell_high"] is not None else current.sell_high,
            "short_selling": data["short_selling"] if "short_selling" in data and data["short_selling"] is not None else current.short_selling,
            "min_profit_amount": data["min_profit_amount"] if "min_profit_amount" in data and data["min_profit_amount"] is not None else current.min_profit_amount,
            "auto_resume_minutes": data["auto_resume_minutes"] if "auto_resume_minutes" in data and data["auto_resume_minutes"] is not None else current.auto_resume_minutes,
            "max_daily_loss": data["max_daily_loss"] if "max_daily_loss" in data and data["max_daily_loss"] is not None else current.max_daily_loss,
            "max_consecutive_losses": data["max_consecutive_losses"] if "max_consecutive_losses" in data and data["max_consecutive_losses"] is not None else current.max_consecutive_losses,
            "llm_interval_minutes": data["llm_interval_minutes"] if "llm_interval_minutes" in data and data["llm_interval_minutes"] is not None else current.llm_interval_minutes,
            "fee_rate_us": data["fee_rate_us"] if "fee_rate_us" in data and data["fee_rate_us"] is not None else current.fee_rate_us,
            "fee_rate_hk": data["fee_rate_hk"] if "fee_rate_hk" in data and data["fee_rate_hk"] is not None else current.fee_rate_hk,
            "min_repricing_pct": data["min_repricing_pct"] if "min_repricing_pct" in data and data["min_repricing_pct"] is not None else current.min_repricing_pct,
            "llm_action_cooldown_seconds": data["llm_action_cooldown_seconds"] if "llm_action_cooldown_seconds" in data and data["llm_action_cooldown_seconds"] is not None else current.llm_action_cooldown_seconds,
            "trading_session_mode": data["trading_session_mode"] if "trading_session_mode" in data and data["trading_session_mode"] is not None else getattr(current, "trading_session_mode", "ANY"),
            "margin_safety_factor": data["margin_safety_factor"] if "margin_safety_factor" in data and data["margin_safety_factor"] is not None else getattr(current, "margin_safety_factor", None),
            "report_schedule_enabled": data["report_schedule_enabled"] if "report_schedule_enabled" in data and data["report_schedule_enabled"] is not None else getattr(current, "report_schedule_enabled", False),
            "report_schedule_interval_hours": data["report_schedule_interval_hours"] if "report_schedule_interval_hours" in data and data["report_schedule_interval_hours"] is not None else getattr(current, "report_schedule_interval_hours", 24),
            "report_schedule_symbol": data["report_schedule_symbol"] if "report_schedule_symbol" in data and data["report_schedule_symbol"] is not None else getattr(current, "report_schedule_symbol", ""),
        }
        try:
            StrategyMergedSchema.model_validate(merged)
        except ValidationError as e:
            raise HTTPException(status_code=422, detail=str(e)) from e
        config, diff = svc.update_config(merged)
        # Surface cross-field inconsistencies (e.g. min_profit < round-trip
        # fee) as warnings returned in the response so the UI can flag
        # them. Error-level issues (e.g. sell_high <= buy_low) still
        # raise a 422 above via Pydantic validation; this is a softer
        # check for the fee/profit relationship that does not fail the
        # save outright — the user may have a good reason.
        consistency_issues = validate_strategy_consistency(config)
        if consistency_issues:
            logger.warning(
                "strategy config has %d consistency issue(s)",
                len(consistency_issues),
            )
        _reload_strategy_after_save()
        response = StrategyResponse.model_validate(config)
        response_dict = response.model_dump()
        response_dict["consistency_warnings"] = consistency_issues
        return response_dict
    except HTTPException as exc:
        result = "FAILED"
        diff = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        diff = {"detail": str(exc)}
        logger.exception("unexpected strategy update failure")
        raise HTTPException(status_code=500, detail="strategy update failed") from exc
    finally:
        audit.record(
            "STRATEGY_UPDATE",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"changed": diff},
            result=result,
        )


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_api_key())])
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    state = svc.get_primary_runtime_state()
    pnl_result = DailyPnlService(db).calculate(
        trade_day=trade_day_for(config.market),
        to_trade_day=lambda instant=None: trade_day_for(config.market, instant),
    )
    runner = get_runner()
    risk = getattr(runner, "risk", None)
    old_daily_pnl = risk.daily_pnl if risk is not None else state.daily_pnl
    old_consecutive_losses = risk.consecutive_losses if risk is not None else state.consecutive_losses
    old_daily_pnl_date = risk.daily_pnl_date if risk is not None else state.daily_pnl_date
    new_pnl = pnl_result.realized_pnl
    new_losses = pnl_result.consecutive_losses
    same_trade_day = old_daily_pnl_date == pnl_result.trade_day
    optimistic_replay = new_pnl > old_daily_pnl + 1e-9 or new_losses < old_consecutive_losses
    if risk is not None and same_trade_day and not pnl_result.trades and optimistic_replay:
        new_pnl = old_daily_pnl
        new_losses = old_consecutive_losses
    if (
        abs(state.daily_pnl - new_pnl) > 1e-9
        or state.consecutive_losses != new_losses
        or state.daily_pnl_date != pnl_result.trade_day
    ):
        state = svc.update_runtime_state(
            symbol=state.symbol,
            daily_pnl=new_pnl,
            daily_pnl_date=pnl_result.trade_day,
            consecutive_losses=new_losses,
        )
    response = StatusResponse.model_validate(state)
    response.runner_running = runner.is_running
    response.last_action_message = getattr(runner, "last_action_message", "")
    response.trading_session_mode = getattr(config, "trading_session_mode", "ANY") or "ANY"
    response.is_trading_hours = is_trading_hours(config.market)
    return response


@router.get("/diagnostics", response_model=DiagnosticsResponse, dependencies=[Depends(require_api_key())])
def get_diagnostics() -> DiagnosticsResponse:
    return DiagnosticsResponse.model_validate(get_runner().diagnostics())


@router.get("/status/history", response_model=StatusHistoryResponse, dependencies=[Depends(require_api_key())])
def get_status_history(
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    symbol: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> StatusHistoryResponse:
    end_at = to or datetime.now(timezone.utc)
    start_at = from_ or (end_at - timedelta(hours=6))
    normalized_symbol = (symbol or "").strip().upper()
    config = StrategyService(db).get_config()
    primary_symbol = (config.symbol or "").strip().upper()
    include_legacy_empty = bool(normalized_symbol and normalized_symbol == primary_symbol)
    snapshots = RuntimeStateService().query_history(
        db,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
        symbol=normalized_symbol,
        include_legacy_empty=include_legacy_empty,
    )
    points = [
        StatusHistoryPoint(
            symbol=snapshot.symbol or normalized_symbol,
            timestamp=snapshot.created_at,
            engine_state=snapshot.engine_state,
            paused=snapshot.paused,
            kill_switch=snapshot.kill_switch,
            daily_pnl=snapshot.daily_pnl,
            consecutive_losses=snapshot.consecutive_losses,
            last_price=snapshot.last_price,
            last_trigger_price=snapshot.last_trigger_price,
        )
        for snapshot in snapshots
    ]
    if not points:
        if normalized_symbol and normalized_symbol == primary_symbol:
            state = StrategyService(db).get_primary_runtime_state()
        else:
            state = StrategyService(db).get_runtime_state(symbol=normalized_symbol)
        points = [
            StatusHistoryPoint(
                symbol=state.symbol or normalized_symbol,
                timestamp=state.updated_at,
                engine_state=state.engine_state,
                paused=state.paused,
                kill_switch=state.kill_switch,
                daily_pnl=state.daily_pnl,
                consecutive_losses=state.consecutive_losses,
                last_price=state.last_price,
                last_trigger_price=state.last_trigger_price,
            )
        ]

    marker_query = db.query(OrderRecord).filter(OrderRecord.status.in_(("FILLED", "PARTIAL_FILLED")))
    if normalized_symbol:
        marker_query = marker_query.filter(OrderRecord.symbol == normalized_symbol)
    if start_at is not None:
        marker_query = marker_query.filter(OrderRecord.created_at >= start_at)
    if end_at is not None:
        marker_query = marker_query.filter(OrderRecord.created_at <= end_at)
    orders = marker_query.order_by(OrderRecord.created_at.asc()).limit(200).all()
    markers = [
        TradeSignalMarker(
            timestamp=order.filled_at or order.created_at,
            broker_order_id=order.broker_order_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.executed_quantity or order.quantity,
            price=order.executed_price or order.price,
            status=order.status,
        )
        for order in orders
    ]
    return StatusHistoryResponse(points=points, markers=markers)
