from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.core.audit import AuditLogger
from app.database import get_db
from app.schemas import (
    LiveExitChallengerReport,
    SignalEdgeClustered,
    SignalEdgeFirstPassage,
    SignalEdgeFutility,
    SignalEdgePromotion,
    SignalEdgeResponse,
    StrategyV2AdxChallengerRequest,
    StrategyV2AdxChallengerResponse,
    StrategyV2BoundaryNeutralDiagnosticRequest,
    StrategyV2BoundaryNeutralDiagnosticResponse,
    StrategyV2BracketChallengerReport,
    StrategyV2ExitChallengerReport,
    StrategyV2ForwardRegistrationRequest,
    StrategyV2ForwardValidationResponse,
    StrategyV2PortfolioRoutingReport,
    StrategyV2ShadowConfigResponse,
    StrategyV2ShadowConfigUpdate,
    StrategyV2ShadowDecisionPage,
    StrategyV2ShadowEvaluationResponse,
    StrategyV2ShadowReplayRequest,
    StrategyV2ShadowReplayResponse,
    StrategyV2ShadowStatusResponse,
    StrategyV2ShadowTradeResponse,
    StrategyV2ShadowVersionResponse,
    TrustedFrozenAssessmentReport,
)
from app.services.live_exit_challenger_service import LiveExitChallengerService
from app.domain.strategy_v2.signal_edge import (
    DEFAULT_ALPHA,
    DEFAULT_MIN_DISTINCT_DAYS,
    DEFAULT_MIN_RESOLVED_TRADES,
    DEFAULT_T_CRITICAL,
)
from app.services.signal_edge_service import SignalEdgeService
from app.services.strategy_v2_shadow_service import StrategyV2ShadowService
from app.services.trusted_frozen_assessment_service import (
    TrustedFrozenAssessmentService,
)
from app.services.strategy_v2_portfolio_service import (
    StrategyV2PortfolioService,
)


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


@router.post(
    "/prewarm-boundary-neutral",
    response_model=StrategyV2BoundaryNeutralDiagnosticResponse,
)
def compare_prewarm_boundary_neutral(
    payload: StrategyV2BoundaryNeutralDiagnosticRequest,
    db: Session = Depends(get_db),
) -> StrategyV2BoundaryNeutralDiagnosticResponse:
    try:
        return StrategyV2ShadowService(db).compare_boundary_neutral_prewarm(
            payload
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.post(
    "/forward-validation/register",
    response_model=StrategyV2ForwardValidationResponse,
)
def register_forward_validation(
    request: Request,
    payload: StrategyV2ForwardRegistrationRequest,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> StrategyV2ForwardValidationResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    summary: dict[str, Any] = {
        "symbol": payload.symbol,
        "candidate_algorithm_version": payload.candidate_algorithm_version,
        "source_config_version": payload.source_config_version,
    }
    try:
        service = StrategyV2ShadowService(db)
        service.register_forward_validation(payload)
        return service.get_forward_validation(
            payload.symbol,
            candidate_algorithm_version=payload.candidate_algorithm_version,
        )
    except ValueError as exc:
        result = "FAILED"
        summary["detail"] = str(exc)
        raise _bad_request(exc) from exc
    except Exception as exc:
        result = "FAILED"
        summary["detail"] = type(exc).__name__
        raise
    finally:
        audit.record(
            "STRATEGY_V2_FORWARD_VALIDATION_REGISTER",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=summary,
            result=result,
        )


@router.get(
    "/forward-validation",
    response_model=StrategyV2ForwardValidationResponse,
)
def get_forward_validation(
    symbol: str = Query(max_length=50),
    candidate_algorithm_version: str = Query(
        default="strategy-v2-causal-trend-prewarm-v1",
        max_length=100,
    ),
    db: Session = Depends(get_db),
) -> StrategyV2ForwardValidationResponse:
    try:
        return StrategyV2ShadowService(db).get_forward_validation(
            symbol,
            candidate_algorithm_version=candidate_algorithm_version,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get(
    "/frozen-disproof-assessment",
    response_model=TrustedFrozenAssessmentReport,
)
def get_frozen_disproof_assessment(
    request: Request,
    db: Session = Depends(get_db),
) -> dict[str, object]:
    """Read the fixed v3 cohort using only server-owned time and identity."""
    if request.query_params:
        raise HTTPException(
            status_code=400,
            detail="frozen disproof assessment does not accept query authority",
        )
    try:
        return TrustedFrozenAssessmentService(db).get_report()
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get(
    "/exit-challengers",
    response_model=StrategyV2ExitChallengerReport,
)
def get_exit_challengers(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> StrategyV2ExitChallengerReport:
    try:
        return StrategyV2ShadowService(db).get_exit_challengers(symbol)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get(
    "/bracket-challengers",
    response_model=StrategyV2BracketChallengerReport,
)
def get_bracket_challengers(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> StrategyV2BracketChallengerReport:
    try:
        return StrategyV2ShadowService(db).get_bracket_challengers(
            symbol
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get(
    "/live-exit-challengers",
    response_model=LiveExitChallengerReport,
)
def get_live_exit_challengers(
    symbol: str = Query(max_length=50),
    db: Session = Depends(get_db),
) -> LiveExitChallengerReport:
    try:
        return LiveExitChallengerService(db).get_report(symbol)
    except ValueError as exc:
        raise _bad_request(exc) from exc


@router.get(
    "/portfolio-routing",
    response_model=StrategyV2PortfolioRoutingReport,
)
def get_portfolio_routing(
    symbol: str | None = Query(default=None, max_length=50),
    db: Session = Depends(get_db),
) -> StrategyV2PortfolioRoutingReport:
    try:
        return StrategyV2PortfolioService(db).get_report(symbol)
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


@router.get("/signal-edge", response_model=SignalEdgeResponse)
def get_signal_edge(
    symbol: str | None = Query(default=None, max_length=50),
    lookback_days: int = Query(default=90, ge=1, le=365),
    stop_pct: float | None = Query(default=None, gt=0, le=100),
    target_pct: float | None = Query(default=None, gt=0, le=100),
    alpha: float = Query(default=DEFAULT_ALPHA, gt=0, lt=1),
    t_critical: float | None = Query(default=None, gt=0, le=10),
    min_resolved_trades: int = Query(default=DEFAULT_MIN_RESOLVED_TRADES, ge=1),
    min_distinct_days: int = Query(default=DEFAULT_MIN_DISTINCT_DAYS, ge=1),
    db: Session = Depends(get_db),
) -> SignalEdgeResponse:
    """Report whether a signal demonstrates edge before its exits are tuned.

    Read-only aggregation over existing Strategy v2 shadow trades: it never
    promotes a symbol, changes the interval, or places an order.
    """
    try:
        verdict, resolved_stop, resolved_target, resolved_symbol = SignalEdgeService(
            db
        ).assess(
            symbol=symbol,
            lookback_days=lookback_days,
            stop_pct=stop_pct,
            target_pct=target_pct,
            alpha=alpha,
            t_critical=t_critical,
            min_resolved_trades=min_resolved_trades,
            min_distinct_days=min_distinct_days,
        )
    except ValueError as exc:
        raise _bad_request(exc) from exc
    gross = SignalEdgeClustered.model_validate(verdict.gross, from_attributes=True)
    net = SignalEdgeClustered.model_validate(verdict.net, from_attributes=True)
    return SignalEdgeResponse(
        generated_at=datetime.now(timezone.utc),
        symbol=resolved_symbol,
        lookback_days=lookback_days,
        stop_pct=resolved_stop,
        target_pct=resolved_target,
        verdict=verdict.verdict,
        reasons=list(verdict.reasons),
        first_passage=SignalEdgeFirstPassage.model_validate(
            verdict.first_passage,
            from_attributes=True,
        ),
        gross=gross,
        net=net,
        futility=SignalEdgeFutility.model_validate(
            verdict.futility,
            from_attributes=True,
        ),
        promotion=SignalEdgePromotion.model_validate(
            verdict.promotion,
            from_attributes=True,
        ),
        clustered=net,
    )
