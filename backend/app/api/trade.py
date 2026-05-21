from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OrderRecord
from app.runner import get_runner
from app.schemas import AccountResponse, ControlRequest, MessageResponse, OrderResponse
from app.services.account_snapshot_service import AccountSnapshotService
from app.services.strategy_service import StrategyService

router = APIRouter(prefix="/api", tags=["trade"])


@router.get("/orders", response_model=list[OrderResponse])
def get_orders(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(limit).all()
    return [OrderResponse.model_validate(o) for o in orders]


@router.get("/account", response_model=AccountResponse)
def get_account() -> AccountResponse:
    return AccountSnapshotService().get_snapshot(get_runner().broker)


@router.post("/control/start", response_model=MessageResponse)
def start_runner(db: Session = Depends(get_db)) -> MessageResponse:
    runner = get_runner()
    if runner.risk.kill_switch:
        raise HTTPException(status_code=403, detail="Kill switch is active — disable it before starting")
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False)
    started = runner.start()
    if not started:
        return MessageResponse(message="runner is already running or failed to start")
    return MessageResponse(message="runner started")


@router.post("/control/stop", response_model=MessageResponse)
def stop_runner(payload: ControlRequest, db: Session = Depends(get_db)) -> MessageResponse:
    get_runner().risk.pause("manual")
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
    get_runner().risk.pause(payload.reason)
    return MessageResponse(message="trading paused")


@router.post("/control/resume", response_model=MessageResponse)
def resume_trading(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False)
    get_runner().risk.resume()
    return MessageResponse(message="trading resumed")


@router.post("/control/kill-switch", response_model=MessageResponse)
def kill_switch(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    runner = get_runner()
    runner.risk.pause(payload.reason)
    runner.risk.enable_kill_switch(payload.reason)
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=True, paused=True)
    return MessageResponse(message="kill switch activated — trading paused")


@router.post("/control/disable-kill-switch", response_model=MessageResponse)
def disable_kill_switch(db: Session = Depends(get_db)) -> MessageResponse:
    runner = get_runner()
    runner.risk.disable_kill_switch()
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=False)
    return MessageResponse(message="kill switch disabled — trading remains paused, use Resume to re-enable")
