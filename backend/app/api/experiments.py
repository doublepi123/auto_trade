from __future__ import annotations

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.domain.experiment.ab_test_manager import ABTestManager
from app.schemas import (
    ExperimentSummary,
    MessageResponse,
    PromptVersionCreate,
    PromptVersionResponse,
)

router = APIRouter(prefix="/api/experiments", tags=["experiments"])


@router.get("", response_model=List[str])
def list_experiment_names(db: Session = Depends(get_db)) -> list[str]:
    manager = ABTestManager(db)
    return manager.list_experiment_names()


@router.get("/versions", response_model=List[PromptVersionResponse])
def list_versions(db: Session = Depends(get_db)) -> list[PromptVersionResponse]:
    manager = ABTestManager(db)
    return [PromptVersionResponse.model_validate(v) for v in manager.list_versions()]


@router.post("/versions", response_model=PromptVersionResponse)
def create_version(
    payload: PromptVersionCreate,
    db: Session = Depends(get_db),
) -> PromptVersionResponse:
    manager = ABTestManager(db)
    try:
        version = manager.create_version(
            name=payload.name,
            version=payload.version,
            description=payload.description,
            template=payload.template,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Failed to create version: {exc}") from exc
    return PromptVersionResponse.model_validate(version)


@router.post("/versions/{version_id}/activate", response_model=MessageResponse)
def activate_version(
    version_id: int,
    db: Session = Depends(get_db),
) -> MessageResponse:
    manager = ABTestManager(db)
    try:
        manager.activate_version(version_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return MessageResponse(message="activated")


@router.get("/versions/active", response_model=PromptVersionResponse | None)
def get_active_version(db: Session = Depends(get_db)) -> PromptVersionResponse | None:
    manager = ABTestManager(db)
    version = manager.get_active_version()
    if version is None:
        return None
    return PromptVersionResponse.model_validate(version)


@router.get("/{experiment_name}/summary", response_model=list[ExperimentSummary])
def get_experiment_summary(
    experiment_name: str,
    db: Session = Depends(get_db),
) -> list[dict]:
    manager = ABTestManager(db)
    return manager.get_experiment_summary(experiment_name)
