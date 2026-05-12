from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OrderRecord
from app.runner import get_runner
from app.schemas import ControlRequest, MessageResponse, OrderResponse
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["trade"])


@router.get("/orders", response_model=list[OrderResponse])
def get_orders(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(limit).all()
    return [OrderResponse.model_validate(o) for o in orders]


@router.post("/control/start", response_model=MessageResponse)
def start_runner(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False, kill_switch=False)
    get_runner().start()
    return MessageResponse(message="runner started")


@router.post("/control/stop", response_model=MessageResponse)
def stop_runner(payload: ControlRequest, db: Session = Depends(get_db)) -> MessageResponse:
    get_runner().stop()
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    return MessageResponse(message="runner stopped")


@router.post("/control/pause", response_model=MessageResponse)
def pause_trading(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    return MessageResponse(message="trading paused")


@router.post("/control/resume", response_model=MessageResponse)
def resume_trading(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False)
    return MessageResponse(message="trading resumed")


@router.post("/control/kill-switch", response_model=MessageResponse)
def kill_switch(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=True)
    return MessageResponse(message="kill switch activated")
