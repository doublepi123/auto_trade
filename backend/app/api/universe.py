from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.api.deps import extract_actor, get_audit_logger
from app.config import settings
from app.core.audit import AuditLogger
from app.database import get_db
from app.domain.universe_selection.catalog import INDEX_CANDIDATE_CATALOG
from app.models import (
    StrategyConfig,
    StrategyV2ShadowConfig,
    UniverseSelectionCandidate,
    UniverseSelectionRun,
)
from app.runner import get_runner
from app.schemas import (
    UniverseCatalogItem,
    UniversePromotionReadinessResponse,
    UniverseSelectionCandidateResponse,
    UniverseSelectionRefreshResponse,
    UniverseSelectionRunResponse,
)
from app.services.universe_promotion_service import UniversePromotionService
from app.services.universe_selection_service import (
    UniverseRefreshResult,
    UniverseSelectionService,
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
    item_responses = [
        UniverseSelectionCandidateResponse.model_validate(item).model_copy(
            update={
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
