from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import (
    StrategyV2AdxChallengerRequest,
    StrategyV2AdxChallengerResponse,
    StrategyV2ShadowConfigResponse,
    StrategyV2ShadowConfigUpdate,
    StrategyV2ShadowDecisionPage,
    StrategyV2ShadowEvaluationResponse,
    StrategyV2ShadowReplayRequest,
    StrategyV2ShadowReplayResponse,
    StrategyV2ShadowStatusResponse,
    StrategyV2ShadowTradeResponse,
    StrategyV2ShadowVersionResponse,
)
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService


router = APIRouter(
    prefix="/api/strategy-shadow",
    tags=["strategy-v2-shadow"],
    dependencies=[Depends(require_api_key())],
)


def _bad_request(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/config", response_model=StrategyV2ShadowConfigResponse)
def get_shadow_config(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> StrategyV2ShadowConfigResponse:
    try:
        return StrategyV2ShadowService(db).get_config(symbol)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/configs", response_model=list[StrategyV2ShadowConfigResponse])
def list_shadow_configs(
    db: Session = Depends(get_db),
) -> list[StrategyV2ShadowConfigResponse]:
    try:
        return StrategyV2ShadowService(db).list_configs()
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.put("/config", response_model=StrategyV2ShadowConfigResponse)
def update_shadow_config(
    request: Request,
    payload: StrategyV2ShadowConfigUpdate,
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StrategyV2ShadowConfigResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    changed: dict[str, Any] = {}
    try:
        service = StrategyV2ShadowService(db)
        before = service.get_config(symbol).model_dump()
        response = service.update_config(payload, symbol=symbol)
        after = response.model_dump()
        changed = {
            key: {"old": before.get(key), "new": after.get(key)}
            for key in payload.model_fields_set
            if before.get(key) != after.get(key)
        }
        return response
    except ValueError as exc:
        result = "FAILED"
        changed = {"detail": str(exc)}
        raise _bad_request(exc) from exc
    except Exception as exc:
        result = "FAILED"
        changed = {"detail": type(exc).__name__}
        raise
    finally:
        audit.record(
            "STRATEGY_V2_SHADOW_UPDATE",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"changed": changed},
            result=result,
        )


@router.get("/status", response_model=StrategyV2ShadowStatusResponse)
def get_shadow_status(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> StrategyV2ShadowStatusResponse:
    try:
        return StrategyV2ShadowService(db).get_status(symbol)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/versions", response_model=list[StrategyV2ShadowVersionResponse])
def list_shadow_versions(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> list[StrategyV2ShadowVersionResponse]:
    try:
        return StrategyV2ShadowService(db).list_versions(symbol)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/evaluation", response_model=StrategyV2ShadowEvaluationResponse)
def get_shadow_evaluation(
    symbol: str | None = Query(default=None, max_length=50),
    config_version: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
) -> StrategyV2ShadowEvaluationResponse:
    try:
        return StrategyV2ShadowService(db).get_evaluation(symbol, config_version)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/adx-challengers", response_model=StrategyV2AdxChallengerResponse)
def compare_shadow_adx_challengers(
    payload: StrategyV2AdxChallengerRequest,
    db: Session = Depends(get_db),
) -> StrategyV2AdxChallengerResponse:
    try:
        return StrategyV2ShadowService(db).compare_adx_challengers(payload)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/decisions", response_model=StrategyV2ShadowDecisionPage)
def list_shadow_decisions(
    symbol: str | None = Query(default=None, max_length=50),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    action: str | None = Query(default=None, max_length=24),
    from_: datetime | None = Query(default=None, alias="from"),
    to: datetime | None = Query(default=None),
    config_version: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
) -> StrategyV2ShadowDecisionPage:
    try:
        return StrategyV2ShadowService(db).list_decisions(
            symbol=symbol,
            page=page,
            page_size=page_size,
            action=action,
            from_dt=from_,
            to_dt=to,
            config_version=config_version,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get("/trades", response_model=list[StrategyV2ShadowTradeResponse])
def list_shadow_trades(
    symbol: str | None = Query(default=None, max_length=50),
    limit: int = Query(default=200, ge=1, le=500),
    config_version: str | None = Query(default=None, max_length=64),
    db: Session = Depends(get_db),
) -> list[StrategyV2ShadowTradeResponse]:
    try:
        return StrategyV2ShadowService(db).list_trades(
            symbol=symbol,
            limit=limit,
            config_version=config_version,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post("/replay", response_model=StrategyV2ShadowReplayResponse)
def replay_shadow_strategy(
    payload: StrategyV2ShadowReplayRequest,
    db: Session = Depends(get_db),
) -> StrategyV2ShadowReplayResponse:
    try:
        return StrategyV2ShadowService(db).replay(payload)
    except ValueError as exc:
        raise _bad_request(exc) from exc
