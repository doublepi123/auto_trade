from __future__ import annotations

import logging
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import OrderRecord
from app.runner import get_runner
from app.schemas import AccountResponse, CashBalanceSchema, ControlRequest, MessageResponse, OrderResponse, PositionSchema
from app.services.strategy_service import StrategyService
from app.api.auth import require_api_key

router = APIRouter(prefix="/api", tags=["trade"])


@router.get("/orders", response_model=list[OrderResponse], dependencies=[Depends(require_api_key())])
def get_orders(
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> list[OrderResponse]:
    orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).limit(limit).all()
    return [OrderResponse.model_validate(o) for o in orders]


@router.get("/account", response_model=AccountResponse, dependencies=[Depends(require_api_key())])
def get_account() -> AccountResponse:
    runner = get_runner()
    broker = runner.broker
    available = True
    try:
        account = broker.get_account()
        total_assets = float(account.total_assets)
        cash_balances = [
            CashBalanceSchema(
                currency=cb.currency,
                available_cash=float(cb.available_cash),
                frozen_cash=float(cb.frozen_cash),
            )
            for cb in account.cash_balances
        ]
    except Exception:
        logging.getLogger("auto_trade.trade").exception("failed to get account balance")
        available = False
        total_assets = 0.0
        cash_balances = []

    try:
        broker_positions = broker.get_positions()
        positions: list[PositionSchema] = []
        for pos in broker_positions:
            try:
                quote = broker.get_quote(pos.symbol)
                market_value = float(pos.quantity * Decimal(str(quote.last_price)))
            except Exception:
                logging.getLogger("auto_trade.trade").warning("failed to get quote for %s, using avg_price fallback", pos.symbol)
                market_value = float(pos.quantity * pos.avg_price)
            positions.append(PositionSchema(
                symbol=pos.symbol,
                side=pos.side,
                quantity=float(pos.quantity),
                avg_price=float(pos.avg_price),
                market_value=market_value,
            ))
    except Exception:
        logging.getLogger("auto_trade.trade").exception("failed to get positions")
        available = False
        positions = []

    return AccountResponse(
        total_assets=total_assets,
        cash_balances=cash_balances,
        positions=positions,
        available=available,
        error=None if available else "Account data unavailable",
    )


@router.post("/control/start", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def start_runner(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False, kill_switch=False)
    started = get_runner().start()
    if not started:
        return MessageResponse(message="runner is already running or failed to start")
    return MessageResponse(message="runner started")


@router.post("/control/stop", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def stop_runner(payload: ControlRequest, db: Session = Depends(get_db)) -> MessageResponse:
    get_runner().risk.pause("manual")
    get_runner().stop()
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    return MessageResponse(message="runner stopped")


@router.post("/control/pause", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def pause_trading(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=True)
    get_runner().risk.pause(payload.reason)
    return MessageResponse(message="trading paused")


@router.post("/control/resume", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def resume_trading(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(paused=False)
    get_runner().risk.resume()
    return MessageResponse(message="trading resumed")


@router.post("/control/kill-switch", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def kill_switch(
    payload: ControlRequest,
    db: Session = Depends(get_db),
) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=True)
    get_runner().risk.enable_kill_switch(payload.reason)
    return MessageResponse(message="kill switch activated")


@router.post("/control/disable-kill-switch", response_model=MessageResponse, dependencies=[Depends(require_api_key())])
def disable_kill_switch(db: Session = Depends(get_db)) -> MessageResponse:
    svc = StrategyService(db)
    svc.update_runtime_state(kill_switch=False)
    get_runner().risk.disable_kill_switch()
    return MessageResponse(message="kill switch disabled")
