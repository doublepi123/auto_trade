from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import (
    StrategyPresetApplyResult,
    StrategyPresetCreate,
    StrategyPresetOut,
    StrategyPresetPage,
)
from app.services.strategy_preset_service import StrategyPresetService
from app.services.strategy_service import StrategyService

router = APIRouter(
    prefix="/api/strategy-presets",
    tags=["strategy-presets"],
    dependencies=[Depends(require_api_key())],
)


@router.post("", response_model=StrategyPresetOut)
def create_preset(payload: StrategyPresetCreate, db=Depends(get_db)) -> StrategyPresetOut:
    return StrategyPresetService(db).create(payload.name, payload.params)


@router.get("", response_model=StrategyPresetPage)
def list_presets(db=Depends(get_db)) -> StrategyPresetPage:
    items = StrategyPresetService(db).list_presets()
    return StrategyPresetPage(items=items, total=len(items))


@router.get("/{preset_id}", response_model=StrategyPresetOut)
def get_preset(preset_id: int, db=Depends(get_db)) -> StrategyPresetOut:
    out = StrategyPresetService(db).get(preset_id)
    if out is None:
        raise HTTPException(status_code=404, detail="strategy preset not found")
    return out


@router.delete("/{preset_id}", status_code=204)
def delete_preset(preset_id: int, db=Depends(get_db)) -> None:
    StrategyPresetService(db).delete(preset_id)


@router.post("/{preset_id}/apply", response_model=StrategyPresetApplyResult)
def apply_preset(
    preset_id: int,
    request: Request,
    db=Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StrategyPresetApplyResult:
    """Apply a preset's params to the active strategy config (audited write)."""
    actor_hash, source_ip = extract_actor(request)
    params = StrategyPresetService(db).get_params(preset_id)
    if params is None:
        audit.record(
            "STRATEGY_PRESET_APPLY",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary={"preset_id": preset_id, "found": False},
            result="NOT_FOUND",
        )
        raise HTTPException(status_code=404, detail="strategy preset not found")
    _, diff = StrategyService(db).update_config(params)
    audit.record(
        "STRATEGY_PRESET_APPLY",
        severity="INFO",
        actor_hash=actor_hash,
        source_ip=source_ip,
        request_summary={"preset_id": preset_id, "changed": list(diff.keys())},
        result="SUCCESS",
    )
    return StrategyPresetApplyResult(applied=True, changed=list(diff.keys()))
