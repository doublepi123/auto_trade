from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import StatusResponse, StrategyConfigSchema, StrategyResponse
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["strategy"])


@router.get("/strategy", response_model=StrategyResponse)
def get_strategy(db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    config = svc.get_config()
    return StrategyResponse.model_validate(config)


@router.put("/strategy", response_model=StrategyResponse)
def update_strategy(payload: StrategyConfigSchema, db: Session = Depends(get_db)) -> StrategyResponse:
    svc = StrategyService(db)
    current = svc.get_config()
    data = payload.model_dump(exclude_unset=True, exclude_none=True)
    buy_low = data.get("buy_low", current.buy_low)
    sell_high = data.get("sell_high", current.sell_high)
    if buy_low > 0 and sell_high > 0 and sell_high <= buy_low:
        raise HTTPException(status_code=422, detail="sell_high must be greater than buy_low")
    config = svc.update_config(data)
    return StrategyResponse.model_validate(config)


@router.get("/status", response_model=StatusResponse)
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    state = svc.get_runtime_state()
    return StatusResponse.model_validate(state)
