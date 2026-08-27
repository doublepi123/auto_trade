from __future__ import annotations

from dataclasses import asdict
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.config import settings
from app.core.audit import AuditLogger
from app.database import SessionLocal, get_db
from app.domain.universe_selection.catalog import INDEX_CANDIDATE_CATALOG
from app.models import (
    StrategyConfig,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.runner import get_runner
from app.schemas import (
    RangeFitnessItem,
    RangeFitnessResponse,
    UniverseCatalogItem,
    UniverseObservationHealthResponse,
    UniversePromotionReadinessResponse,
    UniverseRotationForwardScorecardResponse,
    UniverseSelectionCandidateResponse,
    UniverseSelectionRefreshResponse,
    UniverseSelectionRunPage,
    UniverseSelectionRunResponse,
)
from app.services.durable_job_lease_service import (
    DurableJobLeaseService,
    LeaseBackendError,
    LeaseLostError,
)
from app.services.range_fitness_service import RangeFitnessService
from app.services.universe_promotion_service import UniversePromotionService
from app.services.rotation_forward_scorecard_service import (
    RotationForwardScorecardService,
)
from app.services.research_observation_health_service import (
    ResearchObservationHealthService,
)
from app.services.universe_run_history_service import UniverseRunHistoryService
from app.services.universe_selection_service import (
    UniverseRefreshResult,
    UniverseSelectionLeaseBusyError,
    UniverseSelectionService,
    minimum_peer_observation_dollar_volume,
    observation_pool_overrides,
    select_exploration_candidates,
)

router = APIRouter(
    prefix="/api/universe",
    tags=["universe"],
    dependencies=[Depends(require_api_key())],
)


def build_universe_selection_service(
    db: Session,
) -> UniverseSelectionService:
    return UniverseSelectionService(
        db,
        get_runner().broker,
        minimum_evaluable_ratio=(
            settings.universe_selection_min_evaluable_ratio
        ),
        minimum_residency_days=(
            settings.universe_selection_min_residency_days
        ),
        apply_to_watchlist=(
            settings.universe_selection_apply_to_watchlist
        ),
        enable_shadow=settings.universe_selection_enable_shadow,
        lease_service=DurableJobLeaseService(
            session_factory=SessionLocal,
            default_ttl_seconds=settings.job_lease_ttl_seconds,
        ),
    )


def _run_response(
    run: UniverseSelectionRun,
    items: list[UniverseSelectionCandidate],
    db: Session,
) -> UniverseSelectionRunResponse:
    strategy = (
        db.query(StrategyConfig)
        .order_by(StrategyConfig.id.desc())
        .first()
    )
    trading_symbol = strategy.symbol if strategy is not None else ""
    enabled_shadow_symbols = {
        row.symbol
        for row in db.query(StrategyV2ShadowConfig)
        .filter(StrategyV2ShadowConfig.enabled.is_(True))
        .all()
    }
    observation_overrides = observation_pool_overrides(db)
    exploration_symbols = (
        {
            item.symbol
            for item in select_exploration_candidates(
                items,
                max_symbols=(
                    settings.universe_selection_exploration_max_symbols
                ),
                max_per_sector=(
                    settings.universe_selection_max_per_sector
                ),
                top_score_challengers=(
                    settings
                    .universe_selection_exploration_top_score_challengers
                ),
                already_observed_symbols=(
                    observation_overrides.already_observed_symbols
                ),
                challenger_excluded_symbols=(
                    observation_overrides.challenger_excluded_symbols
                ),
                unobservable_symbols=(
                    observation_overrides.exploration_excluded_symbols
                ),
                minimum_peer_dollar_volume=(
                    minimum_peer_observation_dollar_volume(
                        settings.universe_selection_min_avg_dollar_volume
                    )
                ),
            )
        }
        if run.status == "COMPLETE"
        else set()
    )
    item_responses = [
        UniverseSelectionCandidateResponse.model_validate(item).model_copy(
            update={
                "exploration_selected": item.symbol
                in exploration_symbols,
                "shadow_enabled": item.symbol
                in enabled_shadow_symbols,
                "is_trading_target": item.symbol == trading_symbol,
            },
        )
        for item in items
    ]
    return UniverseSelectionRunResponse.model_validate(
        {
            column.name: getattr(run, column.name)
            for column in run.__table__.columns
        }
        | {"items": item_responses}
    )


def _refresh_response(
    result: UniverseRefreshResult,
    db: Session,
) -> UniverseSelectionRefreshResponse:
    return UniverseSelectionRefreshResponse(
        run=_run_response(result.run, list(result.items), db),
        exploration_symbols=list(result.exploration_symbols),
        added_symbols=list(result.added_symbols),
        removed_symbols=list(result.removed_symbols),
        retained_symbols=list(result.retained_symbols),
        shadow_enabled_symbols=list(result.shadow_enabled_symbols),
        shadow_disabled_symbols=list(result.shadow_disabled_symbols),
        shadow_failed_symbols=list(result.shadow_failed_symbols),
        applied=result.applied,
        reason=result.reason,
    )


@router.get("/catalog", response_model=list[UniverseCatalogItem])
def get_universe_catalog() -> list[UniverseCatalogItem]:
    return [
        UniverseCatalogItem(
            symbol=item.symbol,
            market=item.market,
            alias=item.alias,
            sector=item.sector,
            memberships=list(item.memberships),
        )
        for item in INDEX_CANDIDATE_CATALOG
    ]


@router.get("/latest", response_model=UniverseSelectionRunResponse)
def get_latest_universe_run(
    db: Session = Depends(get_db),
) -> UniverseSelectionRunResponse:
    service = build_universe_selection_service(db)
    latest = service.latest_run()
    if latest is None:
        raise HTTPException(
            status_code=404,
            detail="no universe selection run available",
        )
    return _run_response(latest, service.items_for_run(latest.id), db)


@router.get("/runs", response_model=UniverseSelectionRunPage)
def list_universe_runs(
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(
        default=50,
        ge=1,
        le=100,
        description="Page size (bounded, max 100)",
    ),
    from_date: date | None = Query(
        default=None,
        description="Inclusive start as-of date (YYYY-MM-DD)",
    ),
    to_date: date | None = Query(
        default=None,
        description="Inclusive end as-of date (YYYY-MM-DD)",
    ),
    db: Session = Depends(get_db),
) -> UniverseSelectionRunPage:
    """Read-only bounded paginated universe-selection run history.

    Uses a DB-only query service over stored ``UniverseSelectionRun`` rows.
    Never invokes selection, quote fetches, refresh, shadow synchronization,
    the broker, or any write. Stable newest-first ordering by authoritative
    ``as_of_date`` then ``created_at`` then ``id``. Page items use the honest
    ``UniverseSelectionRunSummary`` model (no candidate ``items``).
    """
    try:
        return UniverseRunHistoryService(db).list_runs(
            page=page,
            page_size=page_size,
            from_date=from_date,
            to_date=to_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get(
    "/range-fitness",
    response_model=RangeFitnessResponse,
)
def get_range_fitness(
    lookback_days: int = Query(default=3, ge=1, le=30),
    min_samples: int = Query(default=60, ge=1, le=100000),
    trend_unsuitable_pct: float = Query(default=60.0, ge=0, le=100),
    range_suitable_pct: float = Query(default=30.0, ge=0, le=100),
    reach_lookback_days: int = Query(default=30, ge=1, le=90),
    db: Session = Depends(get_db),
) -> RangeFitnessResponse:
    """Report whether each shadow-observed symbol still behaves range-like.

    Read-only aggregation over existing Strategy v2 shadow evidence: it never
    promotes a symbol, changes the interval, or places an order.
    """
    try:
        rows = RangeFitnessService(db).assess(
            lookback_days=lookback_days,
            min_samples=min_samples,
            trend_unsuitable_pct=trend_unsuitable_pct,
            range_suitable_pct=range_suitable_pct,
            reach_lookback_days=reach_lookback_days,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return RangeFitnessResponse(
        generated_at=datetime.now(timezone.utc),
        lookback_days=lookback_days,
        min_samples=min_samples,
        trend_unsuitable_pct=trend_unsuitable_pct,
        range_suitable_pct=range_suitable_pct,
        reach_lookback_days=max(lookback_days, reach_lookback_days),
        items=[RangeFitnessItem(**asdict(row)) for row in rows],
    )


@router.get(
    "/promotion-readiness",
    response_model=UniversePromotionReadinessResponse,
)
def get_universe_promotion_readiness(
    db: Session = Depends(get_db),
) -> UniversePromotionReadinessResponse:
    readiness = UniversePromotionService(db).get_readiness()
    if readiness is None:
        raise HTTPException(
            status_code=404,
            detail="no universe selection run available",
        )
    return readiness


@router.get(
    "/rotation-forward-scorecard",
    response_model=UniverseRotationForwardScorecardResponse,
)
def get_rotation_forward_scorecard(
    db: Session = Depends(get_db),
) -> UniverseRotationForwardScorecardResponse:
    scorecard = RotationForwardScorecardService(db).get_scorecard()
    if scorecard is None:
        raise HTTPException(
            status_code=404,
            detail="no universe selection run available",
        )
    return scorecard


@router.get(
    "/observation-health",
    response_model=UniverseObservationHealthResponse,
)
def get_universe_observation_health(
    db: Session = Depends(get_db),
) -> UniverseObservationHealthResponse:
    return ResearchObservationHealthService(db).get_health()


@router.post("/refresh", response_model=UniverseSelectionRefreshResponse)
def refresh_universe(
    request: Request,
    db: Session = Depends(get_db),
    audit: AuditLogger = Depends(get_audit_logger),
) -> UniverseSelectionRefreshResponse:
    actor_hash, source_ip = extract_actor(request)
    result = "SUCCESS"
    summary: dict[str, object] = {}
    try:
        result_response = build_universe_selection_service(db).refresh()
        response = _refresh_response(result_response, db)
        summary = {
            "run_id": response.run.id,
            "as_of_date": response.run.as_of_date.isoformat(),
            "status": response.run.status,
            "selected_count": response.run.selected_count,
            "coverage_ratio": response.run.coverage_ratio,
            "applied": response.applied,
            "added_symbols": response.added_symbols,
            "removed_symbols": response.removed_symbols,
        }
        if response.applied:
            try:
                # Always reload after an applied reconciliation. If a prior
                # reload failed, the next idempotent refresh must retry even
                # though every symbol is now reported as retained.
                get_runner().reload_strategy()
            except Exception as exc:
                raise HTTPException(
                    status_code=503,
                    detail=(
                        "candidate pool was persisted but the trading runtime "
                        "could not reload; retry this refresh"
                    ),
                ) from exc
        return response
    except UniverseSelectionLeaseBusyError as exc:
        result = "FAILED"
        summary = {"detail": type(exc).__name__}
        raise HTTPException(
            status_code=409,
            detail="universe selection refresh is already running",
            headers={
                "Retry-After": str(
                    settings.job_lease_heartbeat_seconds
                )
            },
        ) from exc
    except (LeaseBackendError, LeaseLostError) as exc:
        result = "FAILED"
        summary = {"detail": type(exc).__name__}
        raise HTTPException(
            status_code=503,
            detail="universe selection lease is temporarily unavailable",
            headers={
                "Retry-After": str(
                    settings.job_lease_heartbeat_seconds
                )
            },
        ) from exc
    except Exception as exc:
        result = "FAILED"
        summary = {"detail": type(exc).__name__}
        raise
    finally:
        audit.record(
            "UNIVERSE_SELECTION_REFRESH",
            severity="INFO",
            actor_hash=actor_hash,
            source_ip=source_ip,
            request_summary=summary,
            result=result,
        )
