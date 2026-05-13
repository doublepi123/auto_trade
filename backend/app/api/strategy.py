from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import StatusResponse, StrategyConfigSchema, StrategyResponse
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["strategy"])


@router.get("/strategy", response_model=StrategyResponse, dependencies=[Depends(require_api_key())])
def get_strategy(db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    return StrategyResponse.model_validate(config)


@router.put("/strategy", response_model=StrategyResponse, dependencies=[Depends(require_api_key())])
def put_strategy(payload: StrategyConfigSchema, db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    current = svc.get_config()
    data = payload.model_dump(exclude_unset=True)
    merged = {
        "symbol": data.get("symbol", current.symbol),
        "market": data.get("market", current.market),
        "buy_low": data.get("buy_low", current.buy_low),
        "sell_high": data.get("sell_high", current.sell_high),
        "short_selling": data.get("short_selling", current.short_selling),
        "max_daily_loss": data.get("max_daily_loss", current.max_daily_loss),
        "max_consecutive_losses": data.get("max_consecutive_losses", current.max_consecutive_losses),
    }
    try:
        StrategyConfigSchema.model_validate(merged)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e))
    config = svc.update_config(data)
    return StrategyResponse.model_validate(config)


@router.get("/status", response_model=StatusResponse, dependencies=[Depends(require_api_key())])
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    state = svc.get_runtime_state()
    return StatusResponse.model_validate(state)
