from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.models import OrderRecord, TradeEvent
from app.runner import get_runner
from app.schemas import AccountResponse, CashBalanceSchema, ControlRequest, MessageResponse, OrderCancelResponse, OrderPageResponse, OrderResponse, PositionSchema, TradeEventPageResponse
from app.services.event_list_service import list_timeline_events
from app.services.strategy_service import StrategyService
from app.services.trade_event_service import decode_event_payload, record_trade_event

router = APIRouter(prefix="/api", tags=["trade"])
logger = logging.getLogger("auto_trade.trade")

_LIVE_ORDER_STATUSES = {"SUBMITTED", "PARTIAL_FILLED"}
_TERMINAL_ORDER_STATUSES = {"FILLED", "REJECTED", "CANCELLED"}


def _is_today(value: datetime | None) -> bool:
    if value is None:
        return False
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone().date() == datetime.now().astimezone().date()


def _local_order_response(order: OrderRecord) -> OrderResponse:
    response = OrderResponse.model_validate(order)
    response.source = "local"
    response.cancellable = response.status in _LIVE_ORDER_STATUSES
    return response


def _float_attr(item: Any, name: str) -> float | None:
    value = getattr(item, name, None)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _broker_order_response(item: Any, local_order: OrderRecord | None = None) -> OrderResponse:
    status = str(getattr(item, "status", "SUBMITTED"))
    created_at = getattr(item, "created_at", None) or (local_order.created_at if local_order else datetime.now(timezone.utc))
    return OrderResponse(
        id=local_order.id if local_order else 0,
        broker_order_id=str(getattr(item, "broker_order_id", "")),
        symbol=str(getattr(item, "symbol", local_order.symbol if local_order else "")),
        side=str(getattr(item, "side", local_order.side if local_order else "")),
        quantity=float(getattr(item, "quantity", local_order.quantity if local_order else 0)),
        price=float(getattr(item, "price", local_order.price if local_order else 0)),
        executed_quantity=_float_attr(item, "executed_quantity"),
        executed_price=_float_attr(item, "executed_price"),
        status=status,
        created_at=created_at,
        filled_at=getattr(item, "filled_at", None) or (local_order.filled_at if local_order else None),
        source="broker",
        cancellable=status in _LIVE_ORDER_STATUSES,
    )


def _paginate_orders(items: list[OrderResponse], *, page: int, page_size: int, scope: str) -> OrderPageResponse:
    total = len(items)
    start = (page - 1) * page_size
    end = start + page_size
    return OrderPageResponse(
        items=items[start:end],
        total=total,
        page=page,
        page_size=page_size,
        scope=scope,
    )


def _order_sort_key(item: OrderResponse) -> float:
    value = item.created_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def _order_event_type_for_status(status: str) -> str:
    if status == "FILLED":
        return "ORDER_FILLED"
    if status == "CANCELLED":
        return "ORDER_CANCELLED"
    if status == "REJECTED":
        return "ORDER_REJECTED"
    return "ORDER_STATUS_CHANGED"


def _update_local_order_from_status(db: Session, order_id: str, status_result: Any) -> None:
    status = str(getattr(status_result, "status", "CANCELLED"))
    order = (
        db.query(OrderRecord)
        .filter(OrderRecord.broker_order_id == order_id)
        .order_by(OrderRecord.id.desc())
        .first()
    )
    if order is None:
        record_trade_event(
            db,
            event_type=_order_event_type_for_status(status),
            broker_order_id=order_id,
            status=status,
            message=f"broker order cancel returned status {status}",
            payload={
                "source": "order_cancel_api",
                "executed_quantity": _float_attr(status_result, "executed_quantity"),
                "executed_price": _float_attr(status_result, "executed_price"),
            },
        )
        db.commit()
        return
    old_status = order.status
    old_executed_quantity = order.executed_quantity
    old_executed_price = order.executed_price
    order.status = status
    executed_quantity = _float_attr(status_result, "executed_quantity")
    executed_price = _float_attr(status_result, "executed_price")
    if executed_quantity is not None:
        order.executed_quantity = executed_quantity
    if executed_price is not None:
        order.executed_price = executed_price
    if status in _TERMINAL_ORDER_STATUSES:
        order.filled_at = datetime.now(timezone.utc)
    changed = (
        old_status != order.status
        or old_executed_quantity != order.executed_quantity
        or old_executed_price != order.executed_price
    )
    if changed:
        record_trade_event(
            db,
            event_type=_order_event_type_for_status(status),
            symbol=order.symbol,
            broker_order_id=order_id,
            side=order.side,
            status=status,
            message=f"order status changed from {old_status} to {status}",
            payload={
                "source": "order_cancel_api",
                "old_status": old_status,
                "old_executed_quantity": old_executed_quantity,
                "old_executed_price": old_executed_price,
                "executed_quantity": order.executed_quantity,
                "executed_price": order.executed_price,
            },
        )
    db.add(order)
    db.commit()


@router.get("/orders", response_model=OrderPageResponse)
def get_orders(
    scope: str = Query(default="today", pattern="^(today|history)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=10, ge=1, le=200),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    refresh: bool = Query(default=False),
    db: Session = Depends(get_db),
) -> OrderPageResponse:
    if limit is not None:
        page_size = limit

    if scope == "today" and refresh:
        try:
            get_runner().sync_today_orders_from_broker(force=True)
        except Exception:
            logging.getLogger("auto_trade.trade").exception("force-refresh today orders failed")

    local_orders = db.query(OrderRecord).order_by(OrderRecord.created_at.desc()).all()
    if scope == "history":
        items = [_local_order_response(order) for order in local_orders]
        return _paginate_orders(items, page=page, page_size=page_size, scope=scope)

    local_today = [order for order in local_orders if _is_today(order.created_at)]
    items = sorted(
        (_local_order_response(order) for order in local_today),
        key=_order_sort_key,
        reverse=True,
    )
    return _paginate_orders(items, page=page, page_size=page_size, scope=scope)


@router.get("/events", response_model=TradeEventPageResponse)
def get_trade_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    limit: Optional[int] = Query(default=None, ge=1, le=200),
    symbol: Optional[str] = Query(default=None, max_length=50),
    event_type: Optional[List[str]] = Query(default=None),
    source: str = Query(default="all", pattern="^(trade|audit|all)$"),
    db: Session = Depends(get_db),
) -> TradeEventPageResponse:
    """Trade + audit timeline. Repeat ``event_type`` for multi-filter ( OR within each row type )."""
    if limit is not None:
        page_size = limit

    items, total = list_timeline_events(
        db,
        source=source,  # type: ignore[arg-type]
        event_types=event_type,
        symbol=symbol,
        page=page,
        page_size=page_size,
    )
    return TradeEventPageResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/events/export")
def export_trade_events(
    format: str = Query(default="csv", pattern="^(csv|json)$"),
    limit: int = Query(default=1000, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> Response:
    events = (
        db.query(TradeEvent)
        .order_by(TradeEvent.created_at.desc(), TradeEvent.id.desc())
        .limit(limit)
        .all()
    )
    rows = [
        {
            "id": event.id,
            "event_type": event.event_type,
            "symbol": event.symbol,
            "broker_order_id": event.broker_order_id,
            "side": event.side,
            "status": event.status,
            "message": event.message,
            "payload": decode_event_payload(event.payload_json),
            "created_at": event.created_at.isoformat(),
        }
        for event in events
    ]
    filename = f"trade-events-{datetime.now().strftime('%Y%m%d-%H%M%S')}.{format}"
    if format == "json":
        return Response(
            content=json.dumps(rows, ensure_ascii=False, default=str),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=[
        "id",
        "event_type",
        "symbol",
        "broker_order_id",
        "side",
        "status",
        "message",
        "payload",
        "created_at",
    ])
    writer.writeheader()
    for row in rows:
        csv_row = {**row, "payload": json.dumps(row["payload"], ensure_ascii=False, default=str)}
        writer.writerow(csv_row)
    return Response(
        content=output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/orders/{order_id}/cancel", response_model=OrderCancelResponse)
def cancel_order(
    order_id: str,
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> OrderCancelResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    summary: dict[str, Any] = {"order_id": order_id}
    try:
        order = db.query(OrderRecord).filter(OrderRecord.broker_order_id == order_id).first()
        if order is not None:
            summary = {
                "symbol": order.symbol,
                "quantity": order.quantity,
                "side": order.side,
            }
        runner = get_runner()
        try:
            cancel_by_id = getattr(runner, "cancel_order_by_id", None)
            if callable(cancel_by_id):
                status_result = cancel_by_id(order_id)
            else:
                status_result = runner.broker.cancel_order(order_id)
        except Exception as exc:
            logging.getLogger("auto_trade.trade").exception("failed to cancel order %s", order_id)
            raise HTTPException(status_code=400, detail=f"cancel order failed: {exc}") from exc

        _update_local_order_from_status(db, order_id, status_result)
        status = str(getattr(status_result, "status", "CANCELLED"))
        return OrderCancelResponse(
            broker_order_id=str(getattr(status_result, "broker_order_id", order_id)),
            status=status,
            message="order cancelled" if status == "CANCELLED" else f"order cancel status: {status}",
        )
    except HTTPException as exc:
        result = "FAILED"
        summary = {"detail": str(exc.detail), **summary}
        raise
    except Exception as exc:
        result = "FAILED"
        summary = {"detail": str(exc), **summary}
        logger.exception("unexpected cancel order failure")
        raise HTTPException(status_code=500, detail="cancel order failed") from exc
    finally:
        audit.record(
            "ORDER_CANCEL",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=summary,
            result=result,
        )


@router.get("/account", response_model=AccountResponse)
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
        position_symbols = sorted({pos.symbol for pos in broker_positions if pos.symbol})
        quote_map: dict[str, float] = {}
        if position_symbols:
            try:
                for quote in broker.get_quotes(position_symbols):
                    if quote.symbol and quote.last_price > 0:
                        quote_map[quote.symbol] = float(quote.last_price)
            except Exception:
                logging.getLogger("auto_trade.trade").warning("batch quote fetch failed; using avg_price fallback")
        positions: list[PositionSchema] = []
        for pos in broker_positions:
            last_price = quote_map.get(pos.symbol)
            if last_price is not None and last_price > 0:
                market_value = float(pos.quantity * Decimal(str(last_price)))
            else:
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


@router.post("/control/start", response_model=MessageResponse)
def start_runner(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {}
    try:
        runner = get_runner()
        if runner.risk.kill_switch:
            result = "FAILED"
            detail = {"detail": "Kill switch is active — disable it before starting"}
            raise HTTPException(status_code=403, detail=detail["detail"])
        svc = StrategyService(db)
        svc.update_runtime_state(paused=False, pause_reason="", paused_at=None, pause_auto_resumable=False)
        started = runner.start()
        if not started:
            detail = {"detail": "runner is already running or failed to start"}
            return MessageResponse(message=detail["detail"])
        return MessageResponse(message="runner started")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected start runner failure")
        raise HTTPException(status_code=500, detail="runner start failed") from exc
    finally:
        audit.record(
            "START",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )


@router.post("/control/stop", response_model=MessageResponse)
def stop_runner(
    request: Request,
    payload: ControlRequest,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {}
    try:
        runner = get_runner()
        runner.risk.pause("manual")
        get_runner().stop()
        svc = StrategyService(db)
        svc.update_runtime_state(
            paused=True,
            pause_reason=runner.risk.pause_reason,
            paused_at=runner.risk.paused_at,
            pause_auto_resumable=runner.risk.pause_auto_resumable,
        )
        return MessageResponse(message="runner stopped")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected stop runner failure")
        raise HTTPException(status_code=500, detail="runner stop failed") from exc
    finally:
        audit.record(
            "STOP",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )


@router.post("/control/pause", response_model=MessageResponse)
def pause_trading(
    request: Request,
    payload: ControlRequest,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {"reason": payload.reason}
    try:
        runner = get_runner()
        runner.risk.pause(payload.reason)
        svc = StrategyService(db)
        svc.update_runtime_state(
            paused=True,
            pause_reason=runner.risk.pause_reason,
            paused_at=runner.risk.paused_at,
            pause_auto_resumable=runner.risk.pause_auto_resumable,
        )
        return MessageResponse(message="trading paused")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected pause trading failure")
        raise HTTPException(status_code=500, detail="pause trading failed") from exc
    finally:
        audit.record(
            "PAUSE",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )


@router.post("/control/resume", response_model=MessageResponse)
def resume_trading(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {}
    try:
        get_runner().risk.resume()
        svc = StrategyService(db)
        svc.update_runtime_state(paused=False, pause_reason="", paused_at=None, pause_auto_resumable=False)
        return MessageResponse(message="trading resumed")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected resume trading failure")
        raise HTTPException(status_code=500, detail="resume trading failed") from exc
    finally:
        audit.record(
            "RESUME",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )


@router.post("/control/kill-switch", response_model=MessageResponse)
def kill_switch(
    request: Request,
    payload: ControlRequest,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {"reason": payload.reason}
    try:
        runner = get_runner()
        runner.risk.pause(payload.reason)
        runner.risk.enable_kill_switch(payload.reason)
        try:
            runner.notifier.notify_risk_event("KILL_SWITCH", payload.reason, severity="CRITICAL")
        except Exception as exc:
            logging.getLogger("auto_trade.trade").warning("kill switch notify failed: %s", exc)
        svc = StrategyService(db)
        svc.update_runtime_state(
            kill_switch=True,
            paused=True,
            pause_reason=runner.risk.pause_reason,
            paused_at=runner.risk.paused_at,
            pause_auto_resumable=runner.risk.pause_auto_resumable,
        )
        return MessageResponse(message="kill switch activated — trading paused")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected kill switch failure")
        raise HTTPException(status_code=500, detail="kill switch failed") from exc
    finally:
        audit.record(
            "KILL_SWITCH",
            severity="CRITICAL",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )


@router.post("/control/disable-kill-switch", response_model=MessageResponse)
def disable_kill_switch(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> MessageResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    detail: dict[str, Any] = {}
    try:
        runner = get_runner()
        runner.risk.disable_kill_switch()
        svc = StrategyService(db)
        svc.update_runtime_state(kill_switch=False)
        return MessageResponse(message="kill switch disabled — trading remains paused, use Resume to re-enable")
    except HTTPException as exc:
        result = "FAILED"
        detail = {"detail": str(exc.detail)}
        raise
    except Exception as exc:
        result = "FAILED"
        detail = {"detail": str(exc)}
        logger.exception("unexpected disable kill switch failure")
        raise HTTPException(status_code=500, detail="disable kill switch failed") from exc
    finally:
        audit.record(
            "DISABLE_KILL_SWITCH",
            severity="WARNING",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=detail,
            result=result,
        )
