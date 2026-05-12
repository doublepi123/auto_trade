from __future__ import annotations

from fastapi import APIRouter, Depends
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
    config = svc.update_config(payload.model_dump(exclude_unset=True))
    return StrategyResponse.model_validate(config)


@router.get("/status", response_model=StatusResponse)
def get_status(db: Session = Depends(get_db)) -> StatusResponse:
    svc = StrategyService(db)
    state = svc.get_runtime_state()
    return StatusResponse.model_validate(state)
