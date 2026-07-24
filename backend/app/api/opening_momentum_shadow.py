from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.api.auth import require_api_key
from app.database import get_db
from app.schemas import (
    OpeningMomentumShadowRunResponse,
    OpeningMomentumShadowStatusResponse,
)
from app.services.opening_momentum_shadow_service import (
    OpeningMomentumShadowService,
)


router = APIRouter(
    prefix="/api/opening-momentum-shadow",
    tags=["opening-momentum-shadow"],
    dependencies=[Depends(require_api_key())],
)


@router.get(
    "/status",
    response_model=OpeningMomentumShadowStatusResponse,
)
def get_opening_momentum_shadow_status(
    db: Session = Depends(get_db),
) -> OpeningMomentumShadowStatusResponse:
    return OpeningMomentumShadowService(db).get_status()


@router.get(
    "/runs",
    response_model=list[OpeningMomentumShadowRunResponse],
)
def list_opening_momentum_shadow_runs(
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
) -> list[OpeningMomentumShadowRunResponse]:
    try:
        return OpeningMomentumShadowService(db).list_runs(limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
