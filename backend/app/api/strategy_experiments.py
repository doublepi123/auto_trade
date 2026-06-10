from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import (
    LLMEvaluationResponse,
    StrategyExperimentCreate,
    StrategyExperimentResponse,
    StrategyExperimentRunPage,
    StrategyExperimentRunRequest,
    StrategyExperimentRunResponse,
)
from app.services.llm_recommendation_evaluator import LLMRecommendationEvaluator
from app.services.strategy_experiment_service import StrategyExperimentService

router = APIRouter(prefix="/api/strategy-experiments", tags=["strategy-experiments"])


def _raise_on_value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    status_code = 404 if msg == "strategy experiment not found" else 400
    return HTTPException(status_code=status_code, detail=msg)


@router.post("", response_model=StrategyExperimentResponse, dependencies=[Depends(require_api_key())])
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


@router.get("/llm-evaluations", response_model=LLMEvaluationResponse)
def list_llm_evaluations(
    symbol: str,
    start: str | None = None,
    end: str | None = None,
    horizon_minutes: int = Query(default=60, ge=5, le=1440),
    db: Session = Depends(get_db),
) -> LLMEvaluationResponse:
    from datetime import datetime

    parsed_start: datetime | None = None
    parsed_end: datetime | None = None
    if start:
        try:
            parsed_start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid start date format") from None
    if end:
        try:
            parsed_end = datetime.fromisoformat(end.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=422, detail="invalid end date format") from None

    evaluator = LLMRecommendationEvaluator(db)
    result = evaluator.evaluate(
        symbol=symbol,
        start=parsed_start,
        end=parsed_end,
        horizon_minutes=horizon_minutes,
    )
    return LLMEvaluationResponse.model_validate(result)


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


@router.post("/{experiment_id}/run", response_model=StrategyExperimentResponse, dependencies=[Depends(require_api_key())])
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
    sort: str = Query("total_return_pct", pattern=r"^(total_return_pct|total_pnl|max_drawdown_pct|win_rate|trade_count|sharpe_ratio|profit_factor|profit_loss_ratio|created_at)$"),
    order: str = Query("desc", pattern=r"^(asc|desc)$"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    db: Session = Depends(get_db),
) -> StrategyExperimentRunPage:
    service = StrategyExperimentService(db)
    try:
        return service.list_runs(experiment_id, sort, order, page, page_size)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc

@router.get("/{experiment_id}/runs/{run_id}", response_model=StrategyExperimentRunResponse)
def get_strategy_experiment_run(
    experiment_id: int,
    run_id: int,
    db: Session = Depends(get_db),
) -> StrategyExperimentRunResponse:
    service = StrategyExperimentService(db)
    try:
        return service.get_run(experiment_id, run_id)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc


@router.get("/{experiment_id}/export")
def export_strategy_experiment(
    experiment_id: int,
    format: str = Query(default="json", pattern=r'^(json|csv)$'),
    db: Session = Depends(get_db),
) -> Response:
    from fastapi.responses import JSONResponse, PlainTextResponse

    service = StrategyExperimentService(db)
    try:
        result = service.export_experiment(experiment_id, format)
        if isinstance(result, str):
            return PlainTextResponse(
                content=result,
                media_type="text/csv",
                headers={
                    "Content-Disposition": f'attachment; filename="experiment-{experiment_id}.csv"'
                },
            )
        return JSONResponse(content=result)
    except ValueError as exc:
        raise _raise_on_value_error(exc) from exc
