from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.database import get_db
from app.runner import AppRunner, get_runner
from app.schemas import StatusResponse, StrategyConfigSchema, StrategyMergedSchema, StrategyResponse
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
    response = StatusResponse.model_validate(state)
    runner = get_runner()
    response.runner_running = runner.is_running
    response.last_action_message = getattr(runner, "last_action_message", "")
    return response
