from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OrderRecord
from app.runner import AppRunner, get_runner
from app.schemas import StatusHistoryPoint, StatusHistoryResponse, StatusResponse, StrategyConfigSchema, StrategyMergedSchema, StrategyResponse, TradeSignalMarker
from app.services.daily_pnl_service import DailyPnlService
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService

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


@router.get("/strategy", response_model=StrategyResponse)
def get_strategy(db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    return StrategyResponse.model_validate(config)


@router.put("/strategy", response_model=StrategyResponse)
def put_strategy(payload: StrategyConfigSchema, db: Session = Depends(get_db)) -> StrategyResponse:
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
        "fee_rate_us": data["fee_rate_us"] if data.get("fee_rate_us") is not None else current.fee_rate_us,
        "fee_rate_hk": data["fee_rate_hk"] if data.get("fee_rate_hk") is not None else current.fee_rate_hk,
        "min_repricing_pct": data["min_repricing_pct"] if data.get("min_repricing_pct") is not None else current.min_repricing_pct,
        "llm_action_cooldown_seconds": data["llm_action_cooldown_seconds"] if data.get("llm_action_cooldown_seconds") is not None else current.llm_action_cooldown_seconds,
    }
    try:
        StrategyMergedSchema.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    config = svc.update_config(merged)
    _reload_strategy_after_save()
    return StrategyResponse.model_validate(config)


@router.get("/status", response_model=StatusResponse)
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    state = svc.get_runtime_state()
    pnl_result = DailyPnlService(db).calculate()
    if (
        abs(state.daily_pnl - pnl_result.realized_pnl) > 1e-9
        or state.consecutive_losses != pnl_result.consecutive_losses
        or state.daily_pnl_date != pnl_result.trade_day
    ):
        state = svc.update_runtime_state(
            daily_pnl=pnl_result.realized_pnl,
            daily_pnl_date=pnl_result.trade_day,
            consecutive_losses=pnl_result.consecutive_losses,
        )
    response = StatusResponse.model_validate(state)
    runner = get_runner()
    response.runner_running = runner.is_running
    response.last_action_message = getattr(runner, "last_action_message", "")
    return response


@router.get("/status/history", response_model=StatusHistoryResponse)
def get_status_history(
    from_: Optional[datetime] = Query(default=None, alias="from"),
    to: Optional[datetime] = Query(default=None),
    limit: int = Query(default=200, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> StatusHistoryResponse:
    end_at = to or datetime.now(timezone.utc)
    start_at = from_ or (end_at - timedelta(hours=6))
    snapshots = RuntimeStateService().query_history(db, start_at=start_at, end_at=end_at, limit=limit)
    points = [
        StatusHistoryPoint(
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
        state = StrategyService(db).get_runtime_state()
        points = [
            StatusHistoryPoint(
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
