from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas import (
    StrategyExperimentCreate,
    StrategyExperimentResponse,
    StrategyExperimentRunPage,
    StrategyExperimentRunRequest,
)
from app.services.strategy_experiment_service import StrategyExperimentService

router = APIRouter(prefix="/api/strategy-experiments", tags=["strategy-experiments"])


def _raise_on_value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    status_code = 404 if msg == "strategy experiment not found" else 400
    return HTTPException(status_code=status_code, detail=msg)


@router.post("", response_model=StrategyExperimentResponse)
def create_strategy_experiment(
    payload: StrategyExperimentCreate,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    service = StrategyExperimentService(db)
    try:
        return service.create_experiment(payload)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc


@router.get("", response_model=list[StrategyExperimentResponse])
def list_strategy_experiments(
    db: Session = Depends(get_db),
) -> list[StrategyExperimentResponse]:
    service = StrategyExperimentService(db)
    return service.list_experiments()


@router.get("/{experiment_id}", response_model=StrategyExperimentResponse)
def get_strategy_experiment(
    experiment_id: int,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    service = StrategyExperimentService(db)
    try:
        return service.get_experiment(experiment_id)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc


@router.post("/{experiment_id}/run", response_model=StrategyExperimentResponse)
def run_strategy_experiment(
    experiment_id: int,
    payload: StrategyExperimentRunRequest,
    db: Session = Depends(get_db),
) -> StrategyExperimentResponse:
    service = StrategyExperimentService(db)
    try:
        return service.run_experiment(experiment_id, payload)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc


@router.get("/{experiment_id}/runs", response_model=StrategyExperimentRunPage)
def list_strategy_experiment_runs(
    experiment_id: int,
    sort: str = "total_return_pct",
    order: str = "desc",
    page: int = 1,
    page_size: int = 20,
    db: Session = Depends(get_db),
) -> StrategyExperimentRunPage:
    service = StrategyExperimentService(db)
    try:
        return service.list_runs(experiment_id, sort, order, page, page_size)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc
