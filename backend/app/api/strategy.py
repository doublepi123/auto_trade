from __future__ import annotations

import logging
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
from app.models import OrderRecord, StrategyConfig
from app.runner import (
    PrimarySwitchBlockedError,
    PrimarySwitchCheckError,
    get_runner,
)
from app.schemas import DiagnosticsResponse, StatusHistoryPoint, StatusHistoryResponse, StatusResponse, StrategyConfigSchema, StrategyMergedSchema, StrategyResponse, TradeSignalMarker
from app.services.daily_pnl_service import DailyPnlService
from app.services.runtime_state_service import RuntimeStateService
from app.services.strategy_service import StrategyService, validate_strategy_consistency
from app.services.strategy_version_service import StrategyVersionService

logger = logging.getLogger("auto_trade.strategy")

router = APIRouter(prefix="/api", tags=["strategy"])


def _reload_strategy_after_save() -> None:
    runner = get_runner()
    try:
        runner.reload_strategy()
    except Exception:
        pause_preserving_operational = getattr(
            runner,
            "pause_for_manual_control",
            None,
        )
        if callable(pause_preserving_operational):
            pause_preserving_operational("strategy runtime reload failed")
        else:
            risk = getattr(runner, "risk", None)
            if risk is not None:
                risk.pause("strategy runtime reload failed", auto_resumable=False)
        raise


def merge_and_validate_strategy_update(
    current: object,
    data: dict[str, Any],
) -> dict[str, Any]:
    """Merge a partial write with the active config through one validation path."""
    merged: dict[str, Any] = {}
    for field_name, field_info in StrategyMergedSchema.model_fields.items():
        if field_name in data and data[field_name] is not None:
            merged[field_name] = data[field_name]
        else:
            merged[field_name] = getattr(current, field_name, field_info.default)
    try:
        return StrategyMergedSchema.model_validate(merged).model_dump()
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def update_strategy_with_runtime_reload(
    svc: StrategyService,
    current: object,
    data: dict[str, Any],
) -> tuple[StrategyConfig, dict[str, Any]]:
    """Validate, persist, and synchronously confirm the live runner update."""
    merged = merge_and_validate_strategy_update(current, data)
    runner = get_runner()
    new_symbol = str(merged["symbol"])
    new_market = str(merged["market"])
    old_symbol = str(getattr(current, "symbol", ""))
    old_market = str(getattr(current, "market", ""))
    if new_symbol != old_symbol or new_market != old_market:
        try:
            runner.assert_primary_switch_safe(new_symbol, new_market)
        except PrimarySwitchBlockedError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except PrimarySwitchCheckError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc

    previous = {
        field_name: getattr(current, field_name, field_info.default)
        for field_name, field_info in StrategyMergedSchema.model_fields.items()
    }
    config, diff = svc.update_config(merged)
    try:
        _reload_strategy_after_save()
    except Exception as exc:
        logger.exception("failed to confirm strategy update in the live runner; rolling back")
        rollback_error: Exception | None = None
        try:
            svc.update_config(previous)
            _reload_strategy_after_save()
        except Exception as rollback_exc:
            rollback_error = rollback_exc
            logger.critical("failed to roll back strategy after live reload failure", exc_info=True)
            try:
                pause_preserving_operational = getattr(
                    runner,
                    "pause_for_manual_control",
                    None,
                )
                if callable(pause_preserving_operational):
                    pause_preserving_operational(
                        "strategy reload and rollback failed"
                    )
                else:
                    runner.risk.pause(
                        "strategy reload and rollback failed",
                        auto_resumable=False,
                    )
            except Exception:
                logger.critical("failed to pause runner after strategy reload failure", exc_info=True)
        detail = "strategy saved state could not be activated and was rolled back"
        if rollback_error is not None:
            detail = "strategy activation failed and rollback could not be confirmed; trading paused"
        raise HTTPException(status_code=503, detail=detail) from exc
    return config, diff


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
        config, diff = update_strategy_with_runtime_reload(svc, current, data)
        # Record an immutable snapshot of the current params so the user
        # can later list/rollback. Only done on the successful path — if
        # update_config raises, the diff=... branch below catches it and
        # this line never runs, so failed saves don't pollute history.
        StrategyVersionService(db).record_version(config, actor_hash=actor_hash)
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
        response = StrategyResponse.model_validate(config)
        response.consistency_warnings = consistency_issues
        return response
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


@router.get("/strategy/versions", dependencies=[Depends(require_api_key())])
def list_strategy_versions(db: Session = Depends(get_db)) -> list[dict[str, Any]]:
    return StrategyVersionService(db).list_versions()


@router.post("/strategy/versions/{version_id}/rollback", dependencies=[Depends(require_api_key())])
def rollback_strategy_version(
    version_id: int,
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> dict[str, Any]:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    try:
        version_svc = StrategyVersionService(db)
        params = version_svc.get_version(version_id)
        if params is None:
            raise HTTPException(status_code=404, detail=f"version {version_id} not found")
        svc = StrategyService(db)
        current = svc.get_config()
        config, _ = update_strategy_with_runtime_reload(svc, current, params)
        # Snapshot the post-rollback state so the rollback itself has a
        # traceable version entry (lets a user undo an accidental rollback).
        version_svc.record_version(config, actor_hash=actor_hash)
        return {
            "rolled_back_to": version_id,
            "symbol": config.symbol,
            "buy_low": config.buy_low,
            "sell_high": config.sell_high,
        }
    except HTTPException:
        result = "FAILED"
        raise
    except Exception as exc:
        result = "FAILED"
        logger.exception("strategy rollback failure")
        raise HTTPException(status_code=500, detail="rollback failed") from exc
    finally:
        audit.record(
            "STRATEGY_ROLLBACK",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"version_id": version_id},
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
    if risk is not None:
        new_pnl, new_losses = DailyPnlService.reconcile_risk_state(
            old_daily_pnl,
            old_consecutive_losses,
            old_daily_pnl_date,
            pnl_result,
        )
    else:
        new_pnl = pnl_result.realized_pnl
        new_losses = pnl_result.consecutive_losses
    if pnl_result.is_complete and (
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
    response.protective_exit_permitted = bool(
        getattr(risk, "protective_exit_permitted", False)
    )
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
